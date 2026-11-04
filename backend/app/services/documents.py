"""Coordinator uploads of candidate evidence into the knowledge base."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Document,
    FinalistCategory,
    Import,
    JobStatus,
    ProcessingStatus,
    SourceType,
    User,
)
from app.services import audit
from app.services.file_types import (
    SUPPORTED_LABELS,
    ZIP,
    ArchiveError,
    FileKind,
    detect_kind,
    expand_archive,
    safe_filename,
)
from app.services.storage import LocalStorage


@dataclass(frozen=True)
class Upload:
    filename: str
    data: bytes


@dataclass(frozen=True)
class SkippedFile:
    filename: str
    reason: str


@dataclass
class UploadResult:
    import_record: Import
    accepted: list[Document] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)


@dataclass(frozen=True)
class _Candidate:
    filename: str
    data: bytes
    kind: FileKind
    archive_name: str | None = None
    archive_member: str | None = None


def _expand(
    uploads: list[Upload], settings: Settings
) -> tuple[list[_Candidate], list[SkippedFile]]:
    candidates: list[_Candidate] = []
    skipped: list[SkippedFile] = []
    unsupported = f"unsupported or unrecognised file type (allowed: {SUPPORTED_LABELS})"

    for upload in uploads:
        name = safe_filename(upload.filename)
        if len(upload.data) > settings.max_file_bytes:
            skipped.append(SkippedFile(name, "file is larger than the size limit"))
            continue
        kind = detect_kind(upload.data)
        if kind is None:
            skipped.append(SkippedFile(name, unsupported))
            continue
        if kind is not ZIP:
            candidates.append(_Candidate(name, upload.data, kind))
            continue

        try:
            members, rejected = expand_archive(
                upload.data,
                max_members=settings.max_archive_members,
                max_member_bytes=settings.max_file_bytes,
                max_total_bytes=settings.max_archive_total_bytes,
            )
        except ArchiveError as exc:
            skipped.append(SkippedFile(name, str(exc)))
            continue
        skipped.extend(SkippedFile(f"{name} › {r.path}", r.reason) for r in rejected)
        for member in members:
            label = f"{name} › {member.path}"
            member_kind = detect_kind(member.data)
            if member_kind is ZIP:
                skipped.append(SkippedFile(label, "archives inside archives are not supported"))
            elif member_kind is None:
                skipped.append(SkippedFile(label, unsupported))
            else:
                candidates.append(
                    _Candidate(
                        safe_filename(member.path),
                        member.data,
                        member_kind,
                        archive_name=name,
                        archive_member=member.path,
                    )
                )
    return candidates, skipped


def store_uploads(
    session: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    finalist_category: FinalistCategory,
    user: User,
    uploads: list[Upload],
    source_type: SourceType,
    idempotency_key: str,
) -> UploadResult:
    """Validate, de-duplicate and store files for one finalist in one category.

    Uploading directly to a chosen finalist is the coordinator's confirmation of the
    candidate-document mapping (FR-03), so documents are stored as confirmed and queued for
    processing (malware scan, extraction, chunking) by the ingestion worker.
    """
    candidates, skipped = _expand(uploads, settings)

    known_hashes = set(
        session.scalars(
            select(Document.sha256_hash).where(
                Document.finalist_category_id == finalist_category.id,
                Document.withdrawn_at.is_(None),
            )
        )
    )
    now = datetime.now(UTC)
    import_record = Import(
        id=uuid.uuid4(),
        idempotency_key=idempotency_key,
        finalist_category_id=finalist_category.id,
        status=JobStatus.SUCCEEDED,
        file_count=len(uploads),
        created_by=user.id,
        completed_at=now,
    )
    session.add(import_record)
    session.flush()  # documents reference the import row
    result = UploadResult(import_record, skipped=skipped)

    written: list[str] = []
    try:
        for candidate in candidates:
            digest = hashlib.sha256(candidate.data).hexdigest()
            if digest in known_hashes:
                result.skipped.append(
                    SkippedFile(candidate.filename, "already uploaded for this candidate")
                )
                continue
            known_hashes.add(digest)

            document_id = uuid.uuid4()
            key = f"originals/{finalist_category.id}/{document_id}{candidate.kind.extension}"
            storage.put(key, candidate.data)
            written.append(key)
            document = Document(
                id=document_id,
                import_id=import_record.id,
                finalist_category_id=finalist_category.id,
                mapping_confirmed_by=user.id,
                mapping_confirmed_at=now,
                filename=candidate.filename,
                storage_key=key,
                sha256_hash=digest,
                mime_type=candidate.kind.mime_type,
                size_bytes=len(candidate.data),
                source_type=source_type,
                processing_status=ProcessingStatus.PENDING,
                archive_name=candidate.archive_name,
                archive_member=candidate.archive_member,
                uploaded_by=user.id,
            )
            session.add(document)
            result.accepted.append(document)

        audit.record(
            session,
            user,
            "documents.uploaded",
            "finalist_category",
            finalist_category.id,
            import_id=str(import_record.id),
            accepted=[str(d.id) for d in result.accepted],
            skipped=[{"filename": s.filename, "reason": s.reason} for s in result.skipped],
        )
        session.commit()
    except Exception:
        session.rollback()
        for key in written:
            storage.delete(key)
        raise

    for document in result.accepted:
        session.refresh(document)
    return result


def withdraw_document(session: Session, document: Document, user: User, reason: str) -> Document:
    document.withdrawn_at = datetime.now(UTC)
    document.withdrawn_by = user.id
    document.withdrawal_reason = reason
    audit.record(session, user, "document.withdrawn", "document", document.id, reason=reason)
    session.commit()
    session.refresh(document)
    return document
