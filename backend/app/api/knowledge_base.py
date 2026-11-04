"""Knowledge base: award categories, their shortlisted candidates, and candidate evidence."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    AwardCategory,
    Claim,
    Document,
    DocumentChunk,
    Finalist,
    FinalistCategory,
    Import,
    ProcessingStatus,
    ReferenceNote,
    RubricCriterion,
    RubricVersion,
    SourceType,
    User,
    VerificationReview,
)
from app.schemas.knowledge_base import (
    CategoryCreate,
    CategoryOut,
    CategoryVisibilityIn,
    ChunkOut,
    CitationCheckOut,
    CitationVerifyRequest,
    DocumentOut,
    FinalistCreate,
    FinalistOut,
    PassageOut,
    ReferenceNoteOut,
    ReferenceNotesIn,
    SearchResultOut,
    SkippedFileOut,
    UploadResultOut,
    WithdrawRequest,
)
from app.security.auth import Admin, Coordinator, CurrentUser
from app.services import audit, consent
from app.services.citations import Citation, verify_citations
from app.services.documents import Upload, store_uploads, withdraw_document
from app.services.http import attachment
from app.services.retrieval import criterion_query, get_retriever
from app.services.storage import LocalStorage, get_storage

router = APIRouter(prefix="/api/v1", tags=["knowledge base"])

SessionDep = Annotated[Session, Depends(get_session)]


def _active_document_count(finalist_category_id):
    return select(func.count(Document.id)).where(
        Document.finalist_category_id == finalist_category_id, Document.withdrawn_at.is_(None)
    )


def _get_category(session: Session, category_id: uuid.UUID) -> AwardCategory:
    category = session.get(AwardCategory, category_id)
    if category is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    return category


def _get_finalist_category(
    session: Session, category_id: uuid.UUID, finalist_id: uuid.UUID
) -> FinalistCategory:
    fc = session.scalar(
        select(FinalistCategory).where(
            FinalistCategory.category_id == category_id,
            FinalistCategory.finalist_id == finalist_id,
        )
    )
    if fc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found in this category")
    return fc


def _finalist_out(
    fc: FinalistCategory, document_count: int, claims_total: int = 0, claims_reviewed: int = 0
) -> FinalistOut:
    return FinalistOut(
        id=fc.finalist.id,
        finalist_category_id=fc.id,
        category_id=fc.category_id,
        full_name=fc.finalist.full_name,
        organisation=fc.finalist.organisation,
        position_title=fc.finalist.position_title,
        eligibility_flagged=fc.eligibility_flagged,
        document_count=document_count,
        claims_total=claims_total,
        claims_reviewed=claims_reviewed,
    )


# --- Categories --------------------------------------------------------------------------------


@router.get("/categories")
def list_categories(
    session: SessionDep, _: CurrentUser, include_hidden: bool = False
) -> list[CategoryOut]:
    """Categories by name; hidden ones (such as test categories) only with include_hidden."""
    finalist_count = (
        select(func.count(FinalistCategory.id))
        .where(FinalistCategory.category_id == AwardCategory.id)
        .scalar_subquery()
    )
    document_count = (
        select(func.count(Document.id))
        .join(FinalistCategory, Document.finalist_category_id == FinalistCategory.id)
        .where(FinalistCategory.category_id == AwardCategory.id, Document.withdrawn_at.is_(None))
        .scalar_subquery()
    )
    query = select(AwardCategory, finalist_count, document_count).order_by(AwardCategory.name)
    if not include_hidden:
        query = query.where(AwardCategory.hidden_at.is_(None))
    rows = session.execute(query)
    return [
        CategoryOut.model_validate(c).model_copy(update={"finalist_count": f, "document_count": d})
        for c, f, d in rows
    ]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(body: CategoryCreate, session: SessionDep, user: Coordinator) -> CategoryOut:
    category = AwardCategory(id=uuid.uuid4(), **body.model_dump())
    session.add(category)
    audit.record(session, user, "category.created", "award_category", category.id, code=body.code)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "a category with this code exists") from exc
    session.refresh(category)
    return CategoryOut.model_validate(category)


@router.put("/categories/{category_id}/visibility")
def set_category_visibility(
    category_id: uuid.UUID, body: CategoryVisibilityIn, session: SessionDep, admin: Admin
) -> CategoryOut:
    """Hide a category from lists, the dashboard and the judges' pack, or show it again. Nothing
    in it changes; it stays reachable by its link."""
    category = _get_category(session, category_id)
    if body.hidden != (category.hidden_at is not None):
        category.hidden_at = datetime.now(UTC) if body.hidden else None
        audit.record(
            session,
            admin,
            "category.hidden" if body.hidden else "category.shown",
            "award_category",
            category.id,
            code=category.code,
        )
        session.commit()
    return CategoryOut.model_validate(category)


@router.get("/categories/{category_id}")
def get_category(category_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> CategoryOut:
    return CategoryOut.model_validate(_get_category(session, category_id))


# --- Candidates (finalists) within a category -------------------------------------------------


@router.get("/categories/{category_id}/finalists")
def list_finalists(
    category_id: uuid.UUID, session: SessionDep, _: CurrentUser
) -> list[FinalistOut]:
    _get_category(session, category_id)
    current_claims = select(func.count(Claim.id)).where(
        Claim.finalist_category_id == FinalistCategory.id, Claim.superseded_at.is_(None)
    )
    reviewed_claims = current_claims.where(exists().where(VerificationReview.claim_id == Claim.id))
    rows = session.execute(
        select(
            FinalistCategory,
            _active_document_count(FinalistCategory.id).scalar_subquery(),
            current_claims.scalar_subquery(),
            reviewed_claims.scalar_subquery(),
        )
        .join(Finalist)
        .where(FinalistCategory.category_id == category_id)
        .order_by(Finalist.full_name)
    )
    out = [_finalist_out(fc, docs, total, reviewed) for fc, docs, total, reviewed in rows]
    for item in out:
        record = consent.current(session, item.finalist_category_id)
        item.consent = (
            None
            if record is None
            else {
                "assessment": record.assessment,
                "ai_processing": record.ai_processing,
                "web_research": record.web_research,
            }
        )
    return out


@router.post("/categories/{category_id}/finalists", status_code=status.HTTP_201_CREATED)
def add_finalist(
    category_id: uuid.UUID, body: FinalistCreate, session: SessionDep, user: Coordinator
) -> FinalistOut:
    _get_category(session, category_id)
    finalist = Finalist(id=uuid.uuid4(), **body.model_dump())
    fc = FinalistCategory(id=uuid.uuid4(), finalist=finalist, category_id=category_id)
    session.add_all([finalist, fc])
    audit.record(
        session, user, "finalist.added", "finalist_category", fc.id, full_name=body.full_name
    )
    session.commit()
    session.refresh(fc)
    return _finalist_out(fc, 0)


@router.get("/categories/{category_id}/finalists/{finalist_id}")
def get_finalist(
    category_id: uuid.UUID, finalist_id: uuid.UUID, session: SessionDep, _: CurrentUser
) -> FinalistOut:
    fc = _get_finalist_category(session, category_id, finalist_id)
    count = session.scalar(_active_document_count(fc.id)) or 0
    return _finalist_out(fc, count)


# --- Reference notes ---------------------------------------------------------------------------


def _reference_notes(session: Session, fc: FinalistCategory) -> list[ReferenceNoteOut]:
    rows = session.execute(
        select(ReferenceNote, User.display_name)
        .outerjoin(User, User.id == ReferenceNote.updated_by)
        .where(ReferenceNote.finalist_category_id == fc.id)
        .order_by(ReferenceNote.position)
    ).all()
    return [
        ReferenceNoteOut(
            position=note.position,
            referee_name=note.referee_name,
            notes=note.notes,
            updated_at=note.updated_at,
            updated_by=name,
        )
        for note, name in rows
    ]


@router.get("/categories/{category_id}/finalists/{finalist_id}/reference-notes")
def list_reference_notes(
    category_id: uuid.UUID, finalist_id: uuid.UUID, session: SessionDep, _: CurrentUser
) -> list[ReferenceNoteOut]:
    """What the finalist's references said, as recorded by a coordinator."""
    return _reference_notes(session, _get_finalist_category(session, category_id, finalist_id))


@router.put("/categories/{category_id}/finalists/{finalist_id}/reference-notes")
def save_reference_notes(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    body: ReferenceNotesIn,
    session: SessionDep,
    user: Coordinator,
) -> list[ReferenceNoteOut]:
    """Replace the finalist's reference notes (up to four, in order)."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    if consent.withdrawn(session, fc.id, consent.Purpose.ASSESSMENT):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the finalist has withdrawn consent to assessment (part A)"
        )
    existing = {
        n.position: n
        for n in session.scalars(
            select(ReferenceNote).where(ReferenceNote.finalist_category_id == fc.id)
        )
    }
    now = datetime.now(UTC)
    for position, item in enumerate(body.notes, start=1):
        name, text_ = item.referee_name.strip(), item.notes.strip()
        if not name or not text_:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "each reference needs a name and notes"
            )
        note = existing.pop(position, None)
        if note is None:
            session.add(
                ReferenceNote(
                    finalist_category_id=fc.id,
                    position=position,
                    referee_name=name,
                    notes=text_,
                    updated_by=user.id,
                )
            )
        elif (note.referee_name, note.notes) != (name, text_):
            note.referee_name, note.notes = name, text_
            note.updated_by, note.updated_at = user.id, now
    for note in existing.values():
        session.delete(note)
    audit.record(
        session,
        user,
        "reference_notes.saved",
        "finalist_category",
        fc.id,
        references=len(body.notes),
    )
    session.commit()
    return _reference_notes(session, fc)


# --- Documents ---------------------------------------------------------------------------------


@router.get("/categories/{category_id}/finalists/{finalist_id}/documents")
def list_documents(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    _: CurrentUser,
    include_withdrawn: bool = False,
) -> list[DocumentOut]:
    fc = _get_finalist_category(session, category_id, finalist_id)
    query = select(Document).where(Document.finalist_category_id == fc.id)
    if not include_withdrawn:
        query = query.where(Document.withdrawn_at.is_(None))
    documents = session.scalars(query.order_by(Document.uploaded_at.desc(), Document.filename))
    return [DocumentOut.model_validate(d) for d in documents]


@router.post(
    "/categories/{category_id}/finalists/{finalist_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_documents(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    files: Annotated[list[UploadFile], File(description="PDF, DOCX, XLSX, PNG, JPG or ZIP")],
    idempotency_key: Annotated[str, Header(min_length=8, max_length=200)],
    session: SessionDep,
    storage: Annotated[LocalStorage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Coordinator,
    response: Response,
    source_type: Annotated[SourceType, Form()] = SourceType.SUPPORTING_DOCUMENT,
) -> UploadResultOut:
    fc = _get_finalist_category(session, category_id, finalist_id)

    previous = session.scalar(select(Import).where(Import.idempotency_key == idempotency_key))
    if previous is not None:
        if previous.finalist_category_id != fc.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key already used")
        response.status_code = status.HTTP_200_OK
        documents = session.scalars(select(Document).where(Document.import_id == previous.id))
        return UploadResultOut(
            import_id=previous.id,
            replayed=True,
            accepted=[DocumentOut.model_validate(d) for d in documents],
            skipped=[],
        )

    if consent.withdrawn(session, fc.id, consent.Purpose.ASSESSMENT):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the finalist has withdrawn consent to assessment (part A)"
        )
    if settings.upload_requires_consent and not consent.has(
        session, fc.id, consent.Purpose.ASSESSMENT
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, consent.MESSAGES[consent.Purpose.ASSESSMENT])
    if len(files) > settings.max_files_per_upload:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"upload at most {settings.max_files_per_upload} files at a time",
        )
    # Read one byte past the limit so oversized files are detected without reading them fully.
    uploads = [Upload(f.filename or "", f.file.read(settings.max_file_bytes + 1)) for f in files]

    try:
        result = store_uploads(
            session,
            storage,
            settings,
            finalist_category=fc,
            user=user,
            uploads=uploads,
            source_type=source_type,
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        # A concurrent request used the same key or uploaded the same file.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "conflicting upload in progress; refresh and retry"
        ) from exc

    return UploadResultOut(
        import_id=result.import_record.id,
        accepted=[DocumentOut.model_validate(d) for d in result.accepted],
        skipped=[SkippedFileOut(filename=s.filename, reason=s.reason) for s in result.skipped],
    )


def _get_document(session: Session, document_id: uuid.UUID) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return document


@router.get("/documents/{document_id}/content")
def download_document(
    document_id: uuid.UUID,
    session: SessionDep,
    storage: Annotated[LocalStorage, Depends(get_storage)],
    user: CurrentUser,
) -> Response:
    document = _get_document(session, document_id)
    audit.record(session, user, "document.downloaded", "document", document.id)
    session.commit()
    return attachment(storage.read(document.storage_key), document.filename, document.mime_type)


@router.post("/documents/{document_id}/withdraw")
def withdraw(
    document_id: uuid.UUID, body: WithdrawRequest, session: SessionDep, user: Coordinator
) -> DocumentOut:
    document = _get_document(session, document_id)
    if document.withdrawn_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document is already withdrawn")
    return DocumentOut.model_validate(withdraw_document(session, document, user, body.reason))


@router.get("/documents/{document_id}/chunks")
def list_chunks(document_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> list[ChunkOut]:
    """The extracted text as indexed, for coordinators to check extraction quality."""
    _get_document(session, document_id)
    chunks = session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    )
    return [
        ChunkOut(
            chunk_index=c.chunk_index,
            page_number=c.page_number,
            page_end=c.metadata_.get("page_end"),
            section=c.section,
            token_count=c.token_count,
            content=c.content,
            has_table=bool(c.metadata_.get("has_table")),
            embedded=c.embedding is not None,
        )
        for c in chunks
    ]


@router.post("/documents/{document_id}/reprocess")
def reprocess(document_id: uuid.UUID, session: SessionDep, user: Coordinator) -> DocumentOut:
    """Queue a failed or rejected document for another full processing attempt."""
    document = _get_document(session, document_id)
    if document.withdrawn_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document is withdrawn")
    if document.processing_status not in (ProcessingStatus.FAILED, ProcessingStatus.REJECTED):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "only failed or rejected documents can be reprocessed"
        )
    previous_error = document.processing_error
    document.processing_status = ProcessingStatus.PENDING
    document.processing_attempts = 0
    document.next_attempt_at = None
    document.processing_error = None
    audit.record(
        session,
        user,
        "document.reprocess_requested",
        "document",
        document.id,
        previous_error=previous_error,
    )
    session.commit()
    session.refresh(document)
    return DocumentOut.model_validate(document)


# --- Retrieval ---------------------------------------------------------------------------------


@router.get("/categories/{category_id}/finalists/{finalist_id}/search")
def search_evidence(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
    user: CurrentUser,
    q: Annotated[str | None, Query(min_length=2, max_length=1000)] = None,
    criterion_id: uuid.UUID | None = None,
    top_k: Annotated[int | None, Query(ge=1, le=50)] = None,
) -> SearchResultOut:
    """Hybrid search over one candidate's evidence, by free text or by rubric criterion."""
    if (q is None) == (criterion_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "provide exactly one of q or criterion_id"
        )
    fc = _get_finalist_category(session, category_id, finalist_id)
    if criterion_id is not None:
        criterion = session.scalar(
            select(RubricCriterion)
            .join(RubricVersion)
            .where(RubricCriterion.id == criterion_id, RubricVersion.category_id == category_id)
        )
        if criterion is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "criterion not found in this category")
        q = criterion_query(criterion.name, criterion.description, criterion.evidence_expectations)

    result = get_retriever(settings).search(session, fc.id, q, top_k=top_k)
    audit.record(
        session,
        user,
        "evidence.searched",
        "finalist_category",
        fc.id,
        query=q,
        criterion_id=str(criterion_id) if criterion_id else None,
        returned=[str(p.chunk_id) for p in result.passages],
    )
    session.commit()
    return SearchResultOut(
        query=result.query,
        criterion_id=criterion_id,
        passages=[PassageOut.model_validate(p) for p in result.passages],
        flags=result.flags,
        candidates=result.candidates,
        config=result.config,
    )


@router.post("/categories/{category_id}/finalists/{finalist_id}/citations/verify")
def verify(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    body: CitationVerifyRequest,
    session: SessionDep,
    _: CurrentUser,
) -> list[CitationCheckOut]:
    """Check that each cited passage belongs to this candidate and contains the quote."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    checks = verify_citations(
        session, fc.id, [Citation(c.chunk_id, c.quote) for c in body.citations]
    )
    return [CitationCheckOut.model_validate(c) for c in checks]
