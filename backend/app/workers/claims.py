"""Claim extraction worker: runs queued claim_extraction_runs (FR-06)."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    AwardCategory,
    Claim,
    ClaimEvidence,
    ClaimExtractionRun,
    ClaimStatus,
    EvidenceRelation,
    Finalist,
    FinalistCategory,
    JobStatus,
)
from app.prompts.claim_extraction import PROMPT_VERSION
from app.services import audit, consent
from app.services.citations import normalise
from app.services.claims.extraction import (
    ClaimExtractor,
    ExtractionOutcome,
    load_passages,
    parse_period,
)
from app.services.claims.llm import ClaimLLM, ExtractionLLMError
from app.services.research.matching import ClaimMatcher, match_after_job
from app.services.tracing import trace_job
from app.services.verification import (
    carry_forward,
    external_evidence_by_wording,
    reviewed_claims_by_wording,
    verify_from_supporting_documents,
)

log = logging.getLogger("wob.claims")

CLAIM_SQL = text(
    """
    UPDATE claim_extraction_runs
    SET status = 'running', started_at = now(), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM claim_extraction_runs
        WHERE attempts < :max_attempts
          AND (
              status = 'queued'
              OR (status = 'failed' AND next_attempt_at <= now())
              OR (status = 'running' AND started_at < now() - make_interval(mins => :timeout))
          )
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


@dataclass
class ClaimPipeline:
    sessions: sessionmaker[Session]
    llm: ClaimLLM
    settings: Settings
    # Links auto-accepted criterion sources to the new claims they support.
    matcher: ClaimMatcher | None = None

    def run_once(self) -> bool:
        with self.sessions() as session:
            run_id = session.execute(
                CLAIM_SQL,
                {
                    "max_attempts": self.settings.max_processing_attempts,
                    "timeout": self.settings.processing_timeout_minutes,
                },
            ).scalar_one_or_none()
            session.commit()
        if run_id is None:
            return False
        self.process(run_id)
        return True

    @trace_job("claim extraction", "run_id")
    def process(self, run_id: uuid.UUID) -> None:
        with self.sessions() as session:
            run = session.get_one(ClaimExtractionRun, run_id)
            fc = session.get_one(FinalistCategory, run.finalist_category_id)
            nominee = session.get_one(Finalist, fc.finalist_id).full_name
            category = session.get_one(AwardCategory, fc.category_id).name
            passages = load_passages(session, fc.id)
            ai_consent = consent.has(session, fc.id, consent.Purpose.AI_PROCESSING)
        log.info("extracting claims for %s (%d passages)", fc.id, len(passages))

        if not ai_consent:
            self._fail(run_id, consent.MESSAGES[consent.Purpose.AI_PROCESSING], retry=False)
            return
        if not passages:
            self._fail(run_id, "no indexed evidence for this candidate", retry=False)
            return
        try:
            outcome = ClaimExtractor(self.llm, self.settings.claim_batch_tokens).extract(
                nominee, category, passages
            )
        except ExtractionLLMError as exc:
            log.warning("claim run %s failed: %s", run_id, exc)
            self._fail(run_id, str(exc), retry=exc.retryable)
            return
        except Exception as exc:
            log.exception("claim run %s failed", run_id)
            self._fail(run_id, f"{type(exc).__name__}: {exc}", retry=True)
            return
        self._save(run_id, fc.id, [p.chunk_id for p in passages], outcome)

    def _save(self, run_id, finalist_category_id, chunk_ids, outcome: ExtractionOutcome) -> None:
        now = datetime.now(UTC)
        with self.sessions() as session:
            run = session.get_one(ClaimExtractionRun, run_id, with_for_update=True)
            if run.status != JobStatus.RUNNING:
                log.info("claim run %s was %s meanwhile; discarding results", run_id, run.status)
                return
            # Reviews of the current claims, so identical re-extracted claims keep them.
            previous = reviewed_claims_by_wording(session, finalist_category_id)
            public_evidence = external_evidence_by_wording(session, finalist_category_id)
            carried = 0
            session.execute(
                update(Claim)
                .where(
                    Claim.finalist_category_id == finalist_category_id,
                    Claim.superseded_at.is_(None),
                )
                .values(superseded_at=now)
            )
            for validated in outcome.claims:
                c = validated.claim
                start, start_precision = parse_period(c.period_start)
                end, end_precision = parse_period(c.period_end)
                claim = Claim(
                    id=uuid.uuid4(),
                    finalist_category_id=finalist_category_id,
                    extraction_run_id=run_id,
                    wording=c.statement,
                    claim_type=c.claim_type,
                    entity=c.entity,
                    role=c.role,
                    period_start=start,
                    period_start_precision=start_precision,
                    period_end=end,
                    period_end_precision=end_precision,
                    period_text=c.period_text,
                    quantitative_outcomes=[q.model_dump() for q in c.quantitative_outcomes],
                    limitations=c.limitations,
                    status=ClaimStatus.DISPUTED
                    if validated.contradicting
                    else ClaimStatus.UNVERIFIED,
                    extraction_model=run.llm_model,
                    extraction_prompt_version=run.prompt_version,
                )
                session.add(claim)
                session.flush()
                for relation, items in (
                    (EvidenceRelation.SUPPORTS, validated.supporting),
                    (EvidenceRelation.CONTRADICTS, validated.contradicting),
                ):
                    for passage, quote in items:
                        session.add(
                            ClaimEvidence(
                                claim_id=claim.id,
                                finalist_category_id=finalist_category_id,
                                chunk_id=passage.chunk_id,
                                relation=relation,
                                quoted_span=quote,
                            )
                        )
                # Accepted public sources stay attached to an identical re-extracted claim.
                for source_id, relation, quote in public_evidence.get(normalise(c.statement), []):
                    session.add(
                        ClaimEvidence(
                            claim_id=claim.id,
                            finalist_category_id=finalist_category_id,
                            external_source_id=source_id,
                            relation=relation,
                            quoted_span=quote,
                        )
                    )
                session.flush()
                prior = previous.get(normalise(c.statement))
                if prior and carry_forward(session, claim, prior, bool(validated.contradicting)):
                    carried += 1
            # Jury decision: claims a supporting document supports are verified.
            jury_verified = 0
            if self.settings.jury_verifies_supporting_documents:
                session.flush()
                jury_verified = verify_from_supporting_documents(session, finalist_category_id)
            run.status = JobStatus.SUCCEEDED
            run.completed_at = now
            run.error = None
            run.next_attempt_at = None
            run.input_chunk_ids = chunk_ids
            run.batches = outcome.batches
            run.usage = outcome.usage
            run.served_by_models = outcome.served_by
            run.contradictions = outcome.contradictions
            run.missing_information = outcome.missing_information
            run.discarded = outcome.discarded
            run.claims_created = len(outcome.claims)
            run.claims_carried_forward = carried
            audit.record(
                session,
                None,
                "claims.extracted",
                "claim_extraction_run",
                run_id,
                claims=len(outcome.claims),
                carried_forward=carried,
                jury_verified=jury_verified,
                discarded=len(outcome.discarded),
                served_by=outcome.served_by,
                usage=outcome.usage,
            )
            session.commit()
        log.info(
            "claim run %s: %d claims, %d discarded",
            run_id,
            len(outcome.claims),
            len(outcome.discarded),
        )
        match_after_job(self.sessions, self.matcher, finalist_category_id, only_unmatched=False)

    def _fail(self, run_id: uuid.UUID, message: str, *, retry: bool) -> None:
        with self.sessions() as session:
            run = session.get_one(ClaimExtractionRun, run_id, with_for_update=True)
            run.status = JobStatus.FAILED
            if retry and run.attempts < self.settings.max_processing_attempts:
                run.next_attempt_at = datetime.now(UTC) + timedelta(minutes=2 ** (run.attempts - 1))
            else:
                run.next_attempt_at = None
                if retry:
                    message += f" (gave up after {run.attempts} attempts)"
            run.error = message[:2000]
            run.completed_at = datetime.now(UTC)
            audit.record(
                session,
                None,
                "claims.extraction_failed",
                "claim_extraction_run",
                run_id,
                error=message[:2000],
            )
            session.commit()


def new_run(
    session: Session, finalist_category_id: uuid.UUID, llm_model: str, requested_by: uuid.UUID
) -> ClaimExtractionRun:
    """Queue a run, cancelling earlier failed runs still waiting to retry."""
    session.execute(
        update(ClaimExtractionRun)
        .where(
            ClaimExtractionRun.finalist_category_id == finalist_category_id,
            ClaimExtractionRun.status == JobStatus.FAILED,
            ClaimExtractionRun.next_attempt_at.is_not(None),
        )
        .values(status=JobStatus.CANCELLED, next_attempt_at=None)
    )
    run = ClaimExtractionRun(
        id=uuid.uuid4(),
        finalist_category_id=finalist_category_id,
        llm_model=llm_model,
        prompt_version=PROMPT_VERSION,
        requested_by=requested_by,
    )
    session.add(run)
    return run


def latest_run(session: Session, finalist_category_id: uuid.UUID) -> ClaimExtractionRun | None:
    return session.scalar(
        select(ClaimExtractionRun)
        .where(ClaimExtractionRun.finalist_category_id == finalist_category_id)
        .order_by(ClaimExtractionRun.created_at.desc())
        .limit(1)
    )
