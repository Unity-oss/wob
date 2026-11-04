import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import EvidenceRelation, JobStatus, QualityLevel, ReviewOutcome


class ResearchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    llm_model: str
    served_by_models: list[str]
    prompt_version: str
    criterion_codes: list[str]
    searches: int
    pages_fetched: int
    findings_created: int
    not_found: list[dict[str, Any]]
    identity_concerns: list[str]
    discarded: list[dict[str, Any]]
    usage: dict[str, Any]
    attempts: int
    error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


class SourceOut(BaseModel):
    id: uuid.UUID
    claim_id: uuid.UUID | None
    claim_statement: str | None
    claim_current: bool
    criterion_code: str | None
    criterion_name: str | None
    relation: EvidenceRelation | None
    publisher: str
    url: str
    title: str | None
    published_at: date | None
    retrieved_at: datetime
    quote: str | None
    evidence_passage: str
    source_type: str | None
    identity_confidence: QualityLevel | None
    identity_notes: str | None
    identity_confirmed: bool
    credibility: QualityLevel | None
    reviewer_outcome: ReviewOutcome
    auto_accepted: bool
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_notes: str | None


class ResearchOut(BaseModel):
    research: dict[str, Any]
    latest_run: ResearchRunOut | None
    findings: list[SourceOut]


class SourceReviewIn(BaseModel):
    outcome: ReviewOutcome
    credibility: QualityLevel | None = None
    identity_confirmed: bool = False
    notes: str | None = Field(default=None, max_length=4000)
