"""External research worker (FR-13): runs queued research_runs."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    AwardCategory,
    Claim,
    EvidenceRelation,
    ExternalSource,
    Finalist,
    FinalistCategory,
    JobStatus,
    QualityLevel,
    ResearchRun,
    ReviewOutcome,
)
from app.prompts.external_research import PROMPT_VERSION, SYSTEM_PROMPT, user_prompt
from app.services import audit, consent
from app.services.claims.llm import ExtractionLLMError
from app.services.research.llm import ResearchLLM
from app.services.research.matching import ClaimMatcher, match_after_job
from app.services.research.research import verify_findings
from app.services.rubrics import current_rubric
from app.services.tracing import trace_job
from app.services.verification import auto_accept_external_source

log = logging.getLogger("wob.research")

# Claims most worth checking publicly come first when there are more than the run's limit.
TYPE_PRIORITY = [
    "position",
    "award",
    "outcome",
    "achievement",
    "qualification",
    "membership",
    "advocacy",
    "other",
]

CLAIM_SQL = text(
    """
    UPDATE research_runs
    SET status = 'running', started_at = now(), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM research_runs
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


def research_claims(session: Session, finalist_category_id: uuid.UUID, limit: int) -> list[Claim]:
    priority = case(
        {t: i for i, t in enumerate(TYPE_PRIORITY)},
        value=Claim.claim_type,
        else_=len(TYPE_PRIORITY),
    )
    return list(
        session.scalars(
            select(Claim)
            .where(
                Claim.finalist_category_id == finalist_category_id, Claim.superseded_at.is_(None)
            )
            .order_by(priority, Claim.created_at, Claim.id)
            .limit(limit)
        )
    )


@dataclass
class ResearchPipeline:
    sessions: sessionmaker[Session]
    llm: ResearchLLM
    settings: Settings
    # Links auto-accepted criterion sources to the claims they support.
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

    @trace_job("research", "run_id")
    def process(self, run_id: uuid.UUID) -> None:
        with self.sessions() as session:
            run = session.get_one(ResearchRun, run_id)
            fc = session.get_one(FinalistCategory, run.finalist_category_id)
            finalist = session.get_one(Finalist, fc.finalist_id)
            category = session.get_one(AwardCategory, fc.category_id).name
            claims = research_claims(session, fc.id, self.settings.research_max_claims)
            rubric = current_rubric(session, fc.category_id)
            criteria = list(rubric.criteria) if rubric else []
            research_consent = consent.has(session, fc.id, consent.Purpose.WEB_RESEARCH)
            refs = {f"C{i}": c.id for i, c in enumerate(claims, start=1)}
            criterion_refs = {f"K{i}": (c.code, c.name) for i, c in enumerate(criteria, start=1)}
            prompt = user_prompt(
                finalist.full_name,
                finalist.organisation,
                category,
                [(f"C{i}", c.wording) for i, c in enumerate(claims, start=1)],
                [
                    (f"K{i}", c.name, c.description, c.evidence_expectations)
                    for i, c in enumerate(criteria, start=1)
                ],
                position=finalist.position_title,
            )
        if not research_consent:
            self._fail(run_id, consent.MESSAGES[consent.Purpose.WEB_RESEARCH], retry=False)
            return
        if not claims and not criteria:
            self._fail(
                run_id,
                "this candidate has no current claims and the category has no rubric to research",
                retry=False,
            )
            return
        try:
            result = self.llm.research(SYSTEM_PROMPT, prompt)
        except ExtractionLLMError as exc:
            log.warning("research run %s failed: %s", run_id, exc)
            self._fail(run_id, str(exc), retry=exc.retryable)
            return
        except Exception as exc:
            log.exception("research run %s failed", run_id)
            self._fail(run_id, f"{type(exc).__name__}: {exc}", retry=True)
            return

        outcome = verify_findings(result, refs, criterion_refs)
        now = datetime.now(UTC)
        with self.sessions() as session:
            run = session.get_one(ResearchRun, run_id, with_for_update=True)
            if run.status != JobStatus.RUNNING:
                return
            # Earlier findings nobody has reviewed yet are replaced by this run's.
            session.execute(
                update(ExternalSource)
                .where(
                    ExternalSource.finalist_category_id == fc.id,
                    ExternalSource.reviewer_outcome == ReviewOutcome.PENDING,
                    ExternalSource.superseded_at.is_(None),
                )
                .values(superseded_at=now)
            )
            sources = [
                ExternalSource(
                    finalist_category_id=fc.id,
                    research_run_id=run_id,
                    claim_id=f.claim_id,
                    criterion_code=f.criterion[0] if f.criterion else None,
                    criterion_name=f.criterion[1] if f.criterion else None,
                    relation=EvidenceRelation(f.relation) if f.relation else None,
                    publisher=f.publisher,
                    url=f.url,
                    title=f.title,
                    published_at=f.published_at,
                    retrieved_at=f.retrieved_at,
                    quote=f.quote,
                    evidence_passage=f.passage,
                    content_sha256=f.content_sha256,
                    source_type=f.source_type,
                    identity_confidence=QualityLevel(f.identity_confidence),
                    identity_notes=f.identity_notes,
                )
                for f in outcome.findings
            ]
            session.add_all(sources)
            session.flush()
            auto_accepted = sum(auto_accept_external_source(session, s) for s in sources)
            run.status = JobStatus.SUCCEEDED
            run.completed_at = now
            run.error = None
            run.next_attempt_at = None
            run.claim_ids = list(refs.values())
            run.criterion_codes = [code for code, _ in criterion_refs.values()]
            run.served_by_models = result.served_by
            run.usage = result.usage
            run.searches = result.searches
            run.pages_fetched = len(result.pages)
            run.findings_created = len(outcome.findings)
            run.not_found = outcome.not_found
            run.identity_concerns = outcome.identity_concerns
            run.discarded = outcome.discarded
            audit.record(
                session,
                None,
                "research.completed",
                "research_run",
                run_id,
                findings=len(outcome.findings),
                auto_accepted=auto_accepted,
                discarded=len(outcome.discarded),
                searches=result.searches,
                pages_fetched=len(result.pages),
            )
            session.commit()
        log.info(
            "research run %s: %d findings (%d auto-accepted), %d discarded",
            run_id,
            len(outcome.findings),
            auto_accepted,
            len(outcome.discarded),
        )
        match_after_job(self.sessions, self.matcher, fc.id, only_unmatched=True)

    def _fail(self, run_id: uuid.UUID, message: str, *, retry: bool) -> None:
        with self.sessions() as session:
            run = session.get_one(ResearchRun, run_id, with_for_update=True)
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
                session, None, "research.failed", "research_run", run_id, error=message[:2000]
            )
            session.commit()


def new_research_run(
    session: Session, finalist_category_id: uuid.UUID, llm_model: str, requested_by: uuid.UUID
) -> ResearchRun:
    session.execute(
        update(ResearchRun)
        .where(
            ResearchRun.finalist_category_id == finalist_category_id,
            ResearchRun.status == JobStatus.FAILED,
            ResearchRun.next_attempt_at.is_not(None),
        )
        .values(status=JobStatus.CANCELLED, next_attempt_at=None)
    )
    run = ResearchRun(
        id=uuid.uuid4(),
        finalist_category_id=finalist_category_id,
        llm_model=llm_model,
        prompt_version=PROMPT_VERSION,
        requested_by=requested_by,
    )
    session.add(run)
    return run
