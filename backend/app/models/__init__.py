"""ORM mappings for the tables used by the application so far (schema lives in migrations/sql)."""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Identity, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> ENUM:
    return ENUM(
        enum_cls, name=name, create_type=False, values_callable=lambda e: [m.value for m in e]
    )


class Role(enum.StrEnum):
    ADMIN = "admin"
    SECRETARIAT = "secretariat"
    REVIEWER = "reviewer"
    JUDGE = "judge"
    COMMITTEE = "committee"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingStatus(enum.StrEnum):
    PENDING = "pending"
    SCANNING = "scanning"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    CHUNKING = "chunking"
    INDEXED = "indexed"
    FAILED = "failed"
    REJECTED = "rejected"


class SourceType(enum.StrEnum):
    NOMINATION_FORM = "nomination_form"
    SUPPORTING_DOCUMENT = "supporting_document"


Timestamp = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str]
    display_name: Mapped[str]
    role: Mapped[Role] = mapped_column(_pg_enum(Role, "user_role"))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    password_hash: Mapped[str | None]
    password_set_at: Mapped[datetime | None] = mapped_column(Timestamp)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(Timestamp)
    last_login_at: Mapped[datetime | None] = mapped_column(Timestamp)
    mfa_secret_encrypted: Mapped[str | None]
    mfa_enabled: Mapped[bool] = mapped_column(default=False)


class RolePermission(Base):
    """A right an administrator has granted a user group (app/security/permissions.py)."""

    __tablename__ = "role_permissions"

    role: Mapped[Role] = mapped_column(_pg_enum(Role, "user_role"), primary_key=True)
    permission: Mapped[str] = mapped_column(primary_key=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str]
    kind: Mapped[str] = mapped_column(default="browser")
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(Timestamp)
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ip_address: Mapped[str | None]
    user_agent: Mapped[str | None]


class UserInvite(Base):
    __tablename__ = "user_invites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(Timestamp)
    used_at: Mapped[datetime | None] = mapped_column(Timestamp)
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)


class AwardCategory(Base):
    __tablename__ = "award_categories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    award_year: Mapped[int] = mapped_column(default=2026)
    code: Mapped[str]
    name: Mapped[str]
    description: Mapped[str | None]
    retention_until: Mapped[date | None] = mapped_column(Date)
    hidden_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    judging_closed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    judging_closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class RubricStatus(enum.StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    FROZEN = "frozen"


class WeightingMode(enum.StrEnum):
    EQUAL = "equal"
    WEIGHTED = "weighted"


class RubricVersion(Base):
    __tablename__ = "rubric_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    version: Mapped[int]
    status: Mapped[RubricStatus] = mapped_column(
        _pg_enum(RubricStatus, "rubric_status"), default=RubricStatus.DRAFT
    )
    weighting: Mapped[WeightingMode] = mapped_column(
        _pg_enum(WeightingMode, "weighting_mode"), default=WeightingMode.EQUAL
    )
    # app.services.scales.RatingScale: 0-3 for new versions, 1-5 for the original ones.
    scale: Mapped[str] = mapped_column(default="zero_to_three")
    eligibility_rules: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    notes: Mapped[str | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(Timestamp)
    frozen_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    criteria: Mapped[list["RubricCriterion"]] = relationship(
        lazy="selectin", order_by="RubricCriterion.position"
    )


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    code: Mapped[str]
    name: Mapped[str]
    description: Mapped[str]
    evidence_expectations: Mapped[str | None]
    weight: Mapped[Decimal | None]
    position: Mapped[int]


class Finalist(Base):
    __tablename__ = "finalists"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str]
    organisation: Mapped[str | None]
    position_title: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class FinalistCategory(Base):
    __tablename__ = "finalist_categories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalists.id"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    eligibility_flagged: Mapped[bool] = mapped_column(default=False)
    eligibility_notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    finalist: Mapped[Finalist] = relationship(lazy="joined")


class Import(Base):
    __tablename__ = "imports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str]
    finalist_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finalist_categories.id")
    )
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    file_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("imports.id"))
    finalist_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finalist_categories.id")
    )
    mapping_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    mapping_confirmed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    filename: Mapped[str]
    storage_key: Mapped[str]
    sha256_hash: Mapped[str]
    mime_type: Mapped[str]
    size_bytes: Mapped[int]
    source_type: Mapped[SourceType] = mapped_column(
        _pg_enum(SourceType, "source_type"), default=SourceType.SUPPORTING_DOCUMENT
    )
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        _pg_enum(ProcessingStatus, "processing_status"), default=ProcessingStatus.PENDING
    )
    processing_error: Mapped[str | None]
    extraction_version: Mapped[str | None]
    extraction_quality: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    archive_name: Mapped[str | None]
    archive_member: Mapped[str | None]
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    withdrawn_at: Mapped[datetime | None] = mapped_column(Timestamp)
    withdrawn_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    withdrawal_reason: Mapped[str | None]
    processing_attempts: Mapped[int] = mapped_column(default=0)
    processing_started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    next_attempt_at: Mapped[datetime | None] = mapped_column(Timestamp)
    processed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class DocumentChunk(Base):
    """content_tsv (generated full-text vector) is maintained by Postgres and not mapped."""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    finalist_category_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    page_number: Mapped[int | None]
    section: Mapped[str | None]
    content: Mapped[str]
    token_count: Mapped[int | None]
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    embedding_model: Mapped[str | None]
    extraction_version: Mapped[str]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class ClaimStatus(enum.StrEnum):
    VERIFIED = "verified"
    CORROBORATED = "corroborated"
    UNVERIFIED = "unverified"
    MISSING = "missing"
    DISPUTED = "disputed"


class EvidenceRelation(enum.StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class ClaimExtractionRun(Base):
    __tablename__ = "claim_extraction_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    llm_model: Mapped[str]
    served_by_models: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    prompt_version: Mapped[str]
    input_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list
    )
    batches: Mapped[int] = mapped_column(default=0)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    contradictions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    missing_information: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    discarded: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    claims_created: Mapped[int] = mapped_column(default=0)
    claims_carried_forward: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None]
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("claim_extraction_runs.id")
    )
    wording: Mapped[str]
    claim_type: Mapped[str | None]
    entity: Mapped[str | None]
    role: Mapped[str | None]
    period_start: Mapped[date | None] = mapped_column(Date)
    period_start_precision: Mapped[str | None]
    period_end: Mapped[date | None] = mapped_column(Date)
    period_end_precision: Mapped[str | None]
    period_text: Mapped[str | None]
    quantitative_outcomes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    limitations: Mapped[str | None]
    status: Mapped[ClaimStatus] = mapped_column(
        _pg_enum(ClaimStatus, "claim_status"), default=ClaimStatus.UNVERIFIED
    )
    extraction_model: Mapped[str | None]
    extraction_prompt_version: Mapped[str | None]
    superseded_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    evidence: Mapped[list["ClaimEvidence"]] = relationship(
        lazy="selectin", order_by="ClaimEvidence.created_at"
    )


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"))
    finalist_category_id: Mapped[uuid.UUID]
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_chunks.id"))
    external_source_id: Mapped[uuid.UUID | None]
    relation: Mapped[EvidenceRelation] = mapped_column(
        _pg_enum(EvidenceRelation, "evidence_relation")
    )
    quoted_span: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class EvidenceSufficiency(enum.StrEnum):
    SUFFICIENT = "sufficient"
    LIMITED = "limited"
    INSUFFICIENT = "insufficient"


class QualityLevel(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class VerificationReview(Base):
    """Append-only: a claim's history of reviewer decisions. A decision made automatically under
    a jury decision records its basis instead of a reviewer."""

    __tablename__ = "verification_reviews"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    basis: Mapped[str | None]
    previous_status: Mapped[ClaimStatus | None] = mapped_column(
        _pg_enum(ClaimStatus, "claim_status")
    )
    decision: Mapped[ClaimStatus] = mapped_column(_pg_enum(ClaimStatus, "claim_status"))
    notes: Mapped[str | None]
    sufficiency: Mapped[EvidenceSufficiency | None] = mapped_column(
        _pg_enum(EvidenceSufficiency, "evidence_sufficiency")
    )
    credibility: Mapped[QualityLevel | None] = mapped_column(
        _pg_enum(QualityLevel, "quality_level")
    )
    carried_from: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("verification_reviews.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    reviewer: Mapped[User | None] = relationship(lazy="joined")


class ContradictionResolution(Base):
    __tablename__ = "contradiction_resolutions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim_extraction_runs.id"))
    contradiction_index: Mapped[int]
    resolution: Mapped[str]
    resolved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    resolver: Mapped[User] = relationship(lazy="joined")


class ReviewOutcome(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    llm_model: Mapped[str]
    served_by_models: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    prompt_version: Mapped[str]
    claim_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    criterion_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    searches: Mapped[int] = mapped_column(default=0)
    pages_fetched: Mapped[int] = mapped_column(default=0)
    findings_created: Mapped[int] = mapped_column(default=0)
    not_found: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    identity_concerns: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    discarded: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None]
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class ExternalSource(Base):
    """A public-web finding about one claim or one award criterion, pending a reviewer's
    acceptance unless it was accepted automatically as coming from a credible source."""

    __tablename__ = "external_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    research_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("research_runs.id"))
    claim_id: Mapped[uuid.UUID | None]
    criterion_code: Mapped[str | None]
    criterion_name: Mapped[str | None]
    relation: Mapped[EvidenceRelation | None] = mapped_column(
        _pg_enum(EvidenceRelation, "evidence_relation")
    )
    publisher: Mapped[str]
    url: Mapped[str]
    title: Mapped[str | None]
    published_at: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(Timestamp)
    quote: Mapped[str | None]
    evidence_passage: Mapped[str]
    content_sha256: Mapped[str | None]
    source_type: Mapped[str | None]
    identity_confidence: Mapped[QualityLevel | None] = mapped_column(
        _pg_enum(QualityLevel, "quality_level")
    )
    identity_notes: Mapped[str | None]
    identity_confirmed: Mapped[bool] = mapped_column(default=False)
    credibility: Mapped[QualityLevel | None] = mapped_column(
        _pg_enum(QualityLevel, "quality_level")
    )
    reviewer_outcome: Mapped[ReviewOutcome] = mapped_column(
        _pg_enum(ReviewOutcome, "review_outcome"), default=ReviewOutcome.PENDING
    )
    auto_accepted: Mapped[bool] = mapped_column(default=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    review_notes: Mapped[str | None]
    superseded_at: Mapped[datetime | None] = mapped_column(Timestamp)
    claims_matched_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class AssessmentStatus(enum.StrEnum):
    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssessmentRun(Base):
    __tablename__ = "assessment_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    rubric_status: Mapped[RubricStatus | None] = mapped_column(
        _pg_enum(RubricStatus, "rubric_status")
    )
    idempotency_key: Mapped[str]
    llm_model: Mapped[str]
    prompt_version: Mapped[str]
    embedding_model: Mapped[str]
    reranker_model: Mapped[str | None]
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    items_total: Mapped[int] = mapped_column(default=0)
    items_done: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    served_by_models: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None]
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class CriterionAssessment(Base):
    __tablename__ = "criterion_assessments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessment_runs.id"))
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    criterion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_criteria.id"))
    rating: Mapped[int]
    rationale: Mapped[str]
    limitations: Mapped[str | None]
    sufficiency: Mapped[EvidenceSufficiency] = mapped_column(
        _pg_enum(EvidenceSufficiency, "evidence_sufficiency")
    )
    verification_flags: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    review_required: Mapped[bool] = mapped_column(default=True)
    status: Mapped[AssessmentStatus] = mapped_column(
        _pg_enum(AssessmentStatus, "assessment_status"), default=AssessmentStatus.PROPOSED
    )
    reviewer_rating: Mapped[int | None]
    review_notes: Mapped[str | None]
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(default="ai")
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    citations: Mapped[list["AssessmentCitation"]] = relationship(
        lazy="selectin", order_by="AssessmentCitation.created_at"
    )

    @property
    def effective_rating(self) -> int:
        # 0 is a real rating on the 0-3 scale, so test for None rather than falsiness.
        return self.rating if self.reviewer_rating is None else self.reviewer_rating


class AssessmentCitation(Base):
    __tablename__ = "assessment_citations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    criterion_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("criterion_assessments.id")
    )
    finalist_category_id: Mapped[uuid.UUID]
    chunk_id: Mapped[uuid.UUID | None]
    external_source_id: Mapped[uuid.UUID | None]
    relation: Mapped[EvidenceRelation] = mapped_column(
        _pg_enum(EvidenceRelation, "evidence_relation")
    )
    quoted_span: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class RankingSnapshot(Base):
    """Append-only."""

    __tablename__ = "ranking_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessment_runs.id"))
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    rubric_status: Mapped[RubricStatus] = mapped_column(_pg_enum(RubricStatus, "rubric_status"))
    weighting: Mapped[WeightingMode] = mapped_column(_pg_enum(WeightingMode, "weighting_mode"))
    calculation_version: Mapped[str]
    is_provisional: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    entries: Mapped[list["RankingEntry"]] = relationship(lazy="selectin")


class RankingEntry(Base):
    __tablename__ = "ranking_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ranking_snapshots.id"))
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    score: Mapped[Decimal | None]
    rank: Mapped[int | None]
    is_tie: Mapped[bool] = mapped_column(default=False)
    criteria_assessed: Mapped[int]
    criteria_total: Mapped[int]
    missing_criteria: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    unreviewed_criteria: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    criterion_ratings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ReportKind(enum.StrEnum):
    INDIVIDUAL = "individual"
    CATEGORY = "category"


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[ReportKind] = mapped_column(_pg_enum(ReportKind, "report_kind"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    finalist_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finalist_categories.id")
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ranking_snapshots.id"))
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    storage_key: Mapped[str | None]
    sha256_hash: Mapped[str | None]
    size_bytes: Mapped[int | None]
    generator_version: Mapped[str | None]
    error: Mapped[str | None]
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class CalibrationStudy(Base):
    __tablename__ = "calibration_studies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("award_categories.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessment_runs.id"))
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    name: Mapped[str]
    status: Mapped[str] = mapped_column(default="open")
    outcome: Mapped[str | None]
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(Timestamp)


class CalibrationRater(Base):
    __tablename__ = "calibration_raters"

    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calibration_studies.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    attested_blind: Mapped[bool] = mapped_column(default=False)


class CalibrationItem(Base):
    __tablename__ = "calibration_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calibration_studies.id"))
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    criterion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rubric_criteria.id"))
    position: Mapped[int]


class CalibrationRating(Base):
    __tablename__ = "calibration_ratings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calibration_items.id"))
    rater_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int]
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class FinalistConsent(Base):
    """Append-only; the latest row for a nomination is its current consent."""

    __tablename__ = "finalist_consents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    notice_version: Mapped[str]
    assessment: Mapped[bool]
    ai_processing: Mapped[bool]
    web_research: Mapped[bool]
    method: Mapped[str]
    reference: Mapped[str | None]
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[uuid.UUID | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class JudgeScore(Base):
    """One judge's own score (0-3) and comment for a finalist, given independently."""

    __tablename__ = "judge_scores"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    score: Mapped[int]
    comment: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class ReferenceNote(Base):
    """What one of a finalist's references said, recorded by a coordinator (up to four)."""

    __tablename__ = "reference_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finalist_category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finalist_categories.id"))
    position: Mapped[int]
    referee_name: Mapped[str]
    notes: Mapped[str]
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
