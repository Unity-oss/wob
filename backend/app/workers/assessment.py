"""Criterion assessment worker (FR-08): rates every (finalist, criterion) pair of a run.

Each pair is assessed in its own request that sees only that finalist's evidence (spec §7.1:
score independently before any comparison) and saved as soon as it is done, so a run that
fails part-way resumes where it stopped.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    AssessmentCitation,
    AssessmentRun,
    AwardCategory,
    CriterionAssessment,
    EvidenceRelation,
    EvidenceSufficiency,
    Finalist,
    FinalistCategory,
    JobStatus,
    RubricVersion,
)
from app.prompts.criterion_assessment import system_prompt, user_prompt
from app.services import audit, consent
from app.services.assessment.assess import build_input, check_rating
from app.services.assessment.schema import CriterionRating, rating_output
from app.services.claims.llm import AnthropicClaimLLM, ExtractionLLMError
from app.services.retrieval import Retriever
from app.services.scales import scale_of
from app.services.tracing import trace_job

log = logging.getLogger("wob.assessment")


class AssessorLLM(Protocol):
    model_id: str

    def assess(
        self, system: str, user: str, output: type[CriterionRating] = CriterionRating
    ) -> tuple[CriterionRating, str, dict[str, int]]: ...


class AnthropicAssessor:
    def __init__(self, llm: AnthropicClaimLLM) -> None:
        self.model_id = llm.model_id
        self._llm = llm

    def assess(
        self, system: str, user: str, output: type[CriterionRating] = CriterionRating
    ) -> tuple[CriterionRating, str, dict[str, int]]:
        return self._llm.call(system, user, output)


class ScriptedAssessor:
    """For tests only."""

    model_id = "fake:scripted"

    def __init__(self, respond: Callable[[str, str], CriterionRating | Exception]) -> None:
        self._respond = respond
        self.calls: list[tuple[str, str]] = []

    def assess(
        self, system: str, user: str, output: type[CriterionRating] = CriterionRating
    ) -> tuple[CriterionRating, str, dict[str, int]]:
        self.calls.append((system, user))
        result = self._respond(system, user)
        if isinstance(result, Exception):
            raise result
        return result, "fake", {"input_tokens": len(user) // 4, "output_tokens": 100}


CLAIM_SQL = text(
    """
    UPDATE assessment_runs
    SET status = 'running', started_at = coalesce(started_at, now()), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM assessment_runs
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
class AssessmentPipeline:
    sessions: sessionmaker[Session]
    assessor: AssessorLLM
    retriever: Retriever
    settings: Settings

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

    @trace_job("assessment", "run_id")
    def process(self, run_id: uuid.UUID) -> None:
        with self.sessions() as session:
            run = session.get_one(AssessmentRun, run_id)
            rubric = session.get_one(RubricVersion, run.rubric_version_id)
            category = session.get_one(AwardCategory, run.category_id)
            criteria = list(rubric.criteria)
            finalists = session.execute(
                select(FinalistCategory.id, Finalist.full_name)
                .join(Finalist, Finalist.id == FinalistCategory.finalist_id)
                .where(FinalistCategory.category_id == run.category_id)
                .order_by(Finalist.full_name, FinalistCategory.id)
            ).all()
            done = set(
                session.execute(
                    select(
                        CriterionAssessment.finalist_category_id, CriterionAssessment.criterion_id
                    ).where(CriterionAssessment.run_id == run_id)
                ).all()
            )
            consented = [
                (fc_id, name)
                for fc_id, name in finalists
                if consent.has(session, fc_id, consent.Purpose.AI_PROCESSING)
            ]
            run.skipped = [
                {
                    "finalist_category_id": str(fc_id),
                    "finalist": name,
                    "reason": "no consent to AI processing: assess by human review",
                }
                for fc_id, name in finalists
                if (fc_id, name) not in consented
            ]
            finalists = consented
            run.items_total = len(finalists) * len(criteria)
            run.embedding_model = self.retriever.embedder.model_id
            run.reranker_model = (
                self.retriever.reranker.model_id if self.retriever.reranker else None
            )
            run.retrieval_config = self.retriever.search_config()
            session.commit()
            rubric_label = f"version {rubric.version} ({rubric.status.value})"
            rubric_status = rubric.status
            scale = scale_of(rubric.scale)

        for fc_id, nominee in finalists:
            for criterion in criteria:
                if (fc_id, criterion.id) in done:
                    continue
                try:
                    self._assess_one(
                        run_id,
                        fc_id,
                        nominee,
                        category.name,
                        rubric_label,
                        rubric_status,
                        scale,
                        criterion,
                    )
                except ExtractionLLMError as exc:
                    log.warning("assessment run %s stopped: %s", run_id, exc)
                    self._fail(run_id, f"{nominee} / {criterion.name}: {exc}", retry=exc.retryable)
                    return
                except Exception as exc:
                    log.exception("assessment run %s stopped", run_id)
                    self._fail(
                        run_id,
                        f"{nominee} / {criterion.name}: {type(exc).__name__}: {exc}",
                        retry=True,
                    )
                    return

        with self.sessions() as session:
            run = session.get_one(AssessmentRun, run_id, with_for_update=True)
            run.status = JobStatus.SUCCEEDED
            run.completed_at = datetime.now(UTC)
            run.error = None
            run.next_attempt_at = None
            audit.record(
                session,
                None,
                "assessment.completed",
                "assessment_run",
                run_id,
                items=run.items_done,
                usage=run.usage,
            )
            session.commit()
        log.info("assessment run %s complete", run_id)

    @trace_job("assess criterion", "run_id", "fc_id")
    def _assess_one(
        self, run_id, fc_id, nominee, category, rubric_label, rubric_status, scale, criterion
    ) -> None:
        with self.sessions() as session:
            data = build_input(session, self.retriever, fc_id, criterion)
        prompt = user_prompt(
            nominee=nominee,
            category=category,
            rubric_label=rubric_label,
            criterion=(criterion.name, criterion.description, criterion.evidence_expectations),
            claims=data.claims,
            passages=[(e.ref, e.label, e.text) for e in data.evidence.values() if e.chunk_id],
            sources=[
                (e.ref, e.label, e.text) for e in data.evidence.values() if e.external_source_id
            ],
        )
        rating, served_by, usage = self.assessor.assess(
            system_prompt(scale), prompt, rating_output(scale)
        )
        checked = check_rating(rating, data, rubric_status, scale)

        with self.sessions() as session:
            run = session.get_one(AssessmentRun, run_id, with_for_update=True)
            assessment = CriterionAssessment(
                id=uuid.uuid4(),
                run_id=run_id,
                finalist_category_id=fc_id,
                criterion_id=criterion.id,
                rating=rating.rating,
                rationale=rating.rationale,
                limitations=rating.limitations,
                sufficiency=EvidenceSufficiency(rating.sufficiency),
                verification_flags=checked.flags,
                review_required=checked.review_required,
                raw_output={
                    "model_output": rating.model_dump(),
                    "discarded_citations": checked.discarded,
                    "retrieval": {
                        "query": data.retrieval.query,
                        "flags": data.retrieval.flags,
                        "candidates": data.retrieval.candidates,
                        "chunk_ids": [str(p.chunk_id) for p in data.retrieval.passages],
                    },
                    "served_by": served_by,
                },
            )
            session.add(assessment)
            session.flush()
            for relation, items in (
                (EvidenceRelation.SUPPORTS, checked.supporting),
                (EvidenceRelation.CONTRADICTS, checked.contradicting),
            ):
                for evidence, quote in items:
                    session.add(
                        AssessmentCitation(
                            criterion_assessment_id=assessment.id,
                            finalist_category_id=fc_id,
                            chunk_id=evidence.chunk_id,
                            external_source_id=evidence.external_source_id,
                            relation=relation,
                            quoted_span=quote,
                        )
                    )
            run.items_done += 1
            if served_by not in run.served_by_models:
                run.served_by_models = [*run.served_by_models, served_by]
            merged = dict(run.usage)
            for key, value in usage.items():
                merged[key] = merged.get(key, 0) + value
            run.usage = merged
            session.commit()

    def _fail(self, run_id: uuid.UUID, message: str, *, retry: bool) -> None:
        with self.sessions() as session:
            run = session.get_one(AssessmentRun, run_id, with_for_update=True)
            run.status = JobStatus.FAILED
            if retry and run.attempts < self.settings.max_processing_attempts:
                run.next_attempt_at = datetime.now(UTC) + timedelta(minutes=2 ** (run.attempts - 1))
            else:
                run.next_attempt_at = None
                if retry:
                    message += f" (gave up after {run.attempts} attempts)"
            run.error = message[:2000]
            audit.record(
                session, None, "assessment.failed", "assessment_run", run_id, error=message[:2000]
            )
            session.commit()
