"""Ingestion worker: scan → extract → chunk → embed → index (spec §3 pipeline, FR-02, FR-04).

Jobs are the documents themselves: a worker claims the oldest pending document with
FOR UPDATE SKIP LOCKED, so several workers can run side by side without a separate queue.

Usage: python -m app.workers.ingest [--once | --drain]
"""

import argparse
import hashlib
import logging
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_engine
from app.models import Document, DocumentChunk, ProcessingStatus
from app.services import audit
from app.services.chunking import CHUNKER_VERSION, chunk_blocks
from app.services.embeddings import Embedder, get_embedder
from app.services.extraction import (
    EXTRACTION_VERSION,
    ExtractionResult,
    Ocr,
    UnreadableDocumentError,
    extract,
)
from app.services.scanning import Scanner, ScanResult, get_scanner
from app.services.storage import LocalStorage, StorageIntegrityError, build_storage

log = logging.getLogger("wob.ingest")

CLAIM_SQL = text(
    """
    UPDATE documents
    SET processing_status = 'scanning',
        processing_started_at = now(),
        processing_attempts = processing_attempts + 1
    WHERE id = (
        SELECT id FROM documents
        WHERE withdrawn_at IS NULL
          AND mapping_confirmed_at IS NOT NULL
          AND processing_attempts < :max_attempts
          AND (
              processing_status = 'pending'
              OR (processing_status = 'failed' AND next_attempt_at <= now())
              OR (processing_status IN ('scanning', 'extracting', 'extracted', 'chunking')
                  AND processing_started_at < now() - make_interval(mins => :timeout))
          )
        ORDER BY uploaded_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


class Rejected(Exception):
    """The document can't be used as evidence; retrying won't change that."""

    def __init__(self, message: str, quality: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.quality = quality or {}


@dataclass
class Pipeline:
    sessions: sessionmaker[Session]
    storage: LocalStorage
    scanner: Scanner
    embedder: Embedder
    ocr: Ocr
    settings: Settings

    def run_once(self) -> bool:
        """Process one document if any is waiting. Returns False when the queue is empty."""
        with self.sessions() as session:
            document_id = session.execute(
                CLAIM_SQL,
                {
                    "max_attempts": self.settings.max_processing_attempts,
                    "timeout": self.settings.processing_timeout_minutes,
                },
            ).scalar_one_or_none()
            session.commit()
        if document_id is None:
            return False
        self.process(document_id)
        return True

    def process(self, document_id: uuid.UUID) -> None:
        with self.sessions() as session:
            document = session.get_one(Document, document_id)
            storage_key, expected_hash = document.storage_key, document.sha256_hash
            mime_type, finalist_category_id = document.mime_type, document.finalist_category_id
        log.info("processing document %s (%s)", document_id, mime_type)

        try:
            try:
                data = self.storage.read(storage_key)
            except StorageIntegrityError as exc:
                raise Rejected(f"stored file failed its integrity check: {exc}") from exc
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise Rejected("stored file does not match its recorded SHA-256 hash")

            scan = self.scanner.scan(data)
            if not scan.clean:
                raise Rejected(
                    f"malware detected ({scan.signature})",
                    {
                        "malware_scan": {
                            "scanner": scan.scanner,
                            "result": "infected",
                            "signature": scan.signature,
                        }
                    },
                )

            self._set_status(document_id, ProcessingStatus.EXTRACTING)
            try:
                result = extract(data, mime_type, self.ocr)
            except UnreadableDocumentError as exc:
                raise Rejected(str(exc)) from exc

            self._set_status(document_id, ProcessingStatus.CHUNKING)
            chunks = chunk_blocks(
                result.blocks,
                target_tokens=self.settings.chunk_target_tokens,
                max_tokens=self.settings.chunk_max_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
            )
            vectors = self.embedder.embed_documents([c.content for c in chunks]) if chunks else []
            self._save(document_id, finalist_category_id, result, chunks, vectors, scan)
        except Rejected as exc:
            log.warning("document %s rejected: %s", document_id, exc)
            self._finish_unsuccessfully(document_id, str(exc), rejected=True, quality=exc.quality)
        except Exception as exc:
            log.exception("document %s failed", document_id)
            self._finish_unsuccessfully(document_id, f"{type(exc).__name__}: {exc}", rejected=False)

    def _set_status(self, document_id: uuid.UUID, status: ProcessingStatus) -> None:
        with self.sessions() as session:
            session.execute(
                update(Document).where(Document.id == document_id).values(processing_status=status)
            )
            session.commit()

    def _save(
        self,
        document_id,
        finalist_category_id,
        result: ExtractionResult,
        chunks,
        vectors,
        scan: ScanResult,
    ) -> None:
        with self.sessions() as session:
            document = session.get_one(Document, document_id, with_for_update=True)
            if document.withdrawn_at is not None:
                log.info("document %s was withdrawn during processing; not indexing", document_id)
                return
            session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            for chunk, vector in zip(chunks, vectors, strict=True):
                session.add(
                    DocumentChunk(
                        document_id=document_id,
                        finalist_category_id=finalist_category_id,
                        chunk_index=chunk.index,
                        page_number=chunk.page_start,
                        section=chunk.section,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        embedding=vector,
                        embedding_model=self.embedder.model_id,
                        extraction_version=EXTRACTION_VERSION,
                        metadata_=chunk.metadata,
                    )
                )

            quality = result.quality()
            quality.update(
                chunks=len(chunks),
                chunker_version=CHUNKER_VERSION,
                embedding_model=self.embedder.model_id,
                malware_scan={"scanner": scan.scanner, "result": "clean"},
            )
            if scan.scanner == "disabled":
                quality["warnings"].append("not scanned for malware (scanning disabled)")

            document.processing_status = ProcessingStatus.INDEXED
            document.processing_error = None
            document.extraction_version = EXTRACTION_VERSION
            document.extraction_quality = quality
            document.processed_at = datetime.now(UTC)
            document.next_attempt_at = None
            audit.record(
                session,
                None,
                "document.indexed",
                "document",
                document_id,
                chunks=len(chunks),
                method=result.method,
                warnings=quality["warnings"],
            )
            session.commit()
        log.info("document %s indexed: %d chunks", document_id, len(chunks))

    def _finish_unsuccessfully(
        self,
        document_id: uuid.UUID,
        message: str,
        *,
        rejected: bool,
        quality: dict[str, Any] | None = None,
    ) -> None:
        with self.sessions() as session:
            document = session.get_one(Document, document_id, with_for_update=True)
            if rejected:
                document.processing_status = ProcessingStatus.REJECTED
                document.next_attempt_at = None
            else:
                document.processing_status = ProcessingStatus.FAILED
                attempts = document.processing_attempts
                if attempts >= self.settings.max_processing_attempts:
                    document.next_attempt_at = None
                    message += f" (gave up after {attempts} attempts)"
                else:
                    backoff = timedelta(minutes=2 ** (attempts - 1))
                    document.next_attempt_at = datetime.now(UTC) + backoff
            document.processing_error = message[:2000]
            if quality:
                document.extraction_quality = {**(document.extraction_quality or {}), **quality}
            audit.record(
                session,
                None,
                "document.rejected" if rejected else "document.processing_failed",
                "document",
                document_id,
                error=message[:2000],
            )
            session.commit()


def build_pipeline(settings: Settings) -> Pipeline:
    return Pipeline(
        sessions=sessionmaker(get_engine(), expire_on_commit=False),
        storage=build_storage(settings),
        scanner=get_scanner(settings),
        embedder=get_embedder(settings),
        ocr=Ocr(settings.ocr_languages),
        settings=settings,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.workers.ingest")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process at most one document")
    mode.add_argument("--drain", action="store_true", help="process until the queue is empty")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # model-download chatter
    settings = get_settings()
    pipeline = build_pipeline(settings)
    if not pipeline.ocr.available():
        log.warning("tesseract is not installed: scanned PDFs and images will fail until it is")
    log.info(
        "worker ready (scanner=%s, embeddings=%s)", settings.scanner, pipeline.embedder.model_id
    )

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("stopping after the current document")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if args.once:
        pipeline.run_once()
        return
    while not stopping:
        if not pipeline.run_once():
            if args.drain:
                return
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
