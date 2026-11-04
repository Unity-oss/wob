import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import ProcessingStatus, SourceType


class CategoryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    award_year: int
    code: str
    name: str
    description: str | None
    hidden_at: datetime | None = None
    finalist_count: int = 0
    document_count: int = 0


class CategoryVisibilityIn(BaseModel):
    hidden: bool


class FinalistCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    organisation: str | None = Field(default=None, max_length=200)
    position_title: str | None = Field(default=None, max_length=200)


class FinalistOut(BaseModel):
    id: uuid.UUID
    finalist_category_id: uuid.UUID
    category_id: uuid.UUID
    full_name: str
    organisation: str | None
    position_title: str | None
    eligibility_flagged: bool
    document_count: int = 0
    claims_total: int = 0
    claims_reviewed: int = 0
    # Current consent (None = none recorded): {"assessment", "ai_processing", "web_research"}.
    consent: dict[str, bool] | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    sha256_hash: str
    source_type: SourceType
    processing_status: ProcessingStatus
    archive_name: str | None
    archive_member: str | None
    uploaded_at: datetime
    withdrawn_at: datetime | None
    withdrawal_reason: str | None
    processing_error: str | None
    processing_attempts: int
    processed_at: datetime | None
    extraction_quality: dict[str, Any]


class ChunkOut(BaseModel):
    chunk_index: int
    page_number: int | None
    page_end: int | None
    section: str | None
    token_count: int | None
    content: str
    has_table: bool
    embedded: bool


class SkippedFileOut(BaseModel):
    filename: str
    reason: str


class UploadResultOut(BaseModel):
    import_id: uuid.UUID
    replayed: bool = False
    accepted: list[DocumentOut]
    skipped: list[SkippedFileOut]


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class PassageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rank: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    source_type: str
    page_start: int | None
    page_end: int | None
    section: str | None
    content: str
    vector_rank: int | None
    lexical_rank: int | None
    fused_score: float
    rerank_score: float | None


class SearchResultOut(BaseModel):
    query: str
    criterion_id: uuid.UUID | None = None
    passages: list[PassageOut]
    flags: list[str]
    candidates: dict[str, int]
    config: dict[str, Any]


class CitationIn(BaseModel):
    chunk_id: uuid.UUID
    quote: str = Field(max_length=5000)


class CitationVerifyRequest(BaseModel):
    citations: list[CitationIn] = Field(min_length=1, max_length=200)


class CitationCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: uuid.UUID
    quote: str
    valid: bool
    reason: str | None
    document_id: uuid.UUID | None
    filename: str | None
    page_number: int | None


MAX_REFERENCE_NOTES = 4


class ReferenceNoteIn(BaseModel):
    referee_name: str = Field(min_length=1, max_length=200)
    notes: str = Field(min_length=1, max_length=8000)


class ReferenceNotesIn(BaseModel):
    notes: list[ReferenceNoteIn] = Field(max_length=MAX_REFERENCE_NOTES)


class ReferenceNoteOut(BaseModel):
    position: int
    referee_name: str
    notes: str
    updated_at: datetime
    updated_by: str | None
