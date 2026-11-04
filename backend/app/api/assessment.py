"""Rubrics, criterion assessment (FR-08), quality review and provisional ranking (FR-09, FR-10)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.claims import Reviewer
from app.api.knowledge_base import SessionDep, _get_category, _get_finalist_category
from app.config import Settings, get_settings
from app.models import (
    AssessmentRun,
    AssessmentStatus,
    CriterionAssessment,
    ExternalSource,
    Finalist,
    FinalistCategory,
    JobStatus,
    RankingSnapshot,
    RubricCriterion,
    RubricStatus,
    RubricVersion,
    User,
)
from app.prompts.criterion_assessment import PROMPT_VERSION as ASSESSMENT_PROMPT_VERSION
from app.schemas.assessment import (
    AssessmentCitationOut,
    AssessmentReviewIn,
    AssessmentRunIn,
    AssessmentRunOut,
    CandidateAssessmentsOut,
    CategoryRankingsOut,
    CriterionAssessmentOut,
    CriterionOut,
    HumanRatingIn,
    RankingEntryOut,
    RankingIn,
    RankingOut,
    RubricIn,
    RubricOut,
    UnfreezeIn,
)
from app.security.auth import Admin, Coordinator, CurrentUser, RubricApprover
from app.services import audit, rubrics
from app.services.assessment.review import AssessmentReviewRejected, review_assessment
from app.services.calibration import is_blinded, latest_result
from app.services.claims.llm import provider_status
from app.services.ranking import create_snapshot, display, display_points
from app.services.scales import RatingScale, scale_of

router = APIRouter(prefix="/api/v1", tags=["assessment and ranking"])
BLINDED = (
    "hidden while you are a blind rater in an open calibration study for this category; "
    "submit your ratings first"
)
SettingsDep = Annotated[Settings, Depends(get_settings)]

DISCLAIMER = (
    "AI rankings are advisory. The awards committee retains responsibility for final "
    "deliberation and winner approval."
)
# Adjacent finalists closer than this on the 1-5 scale (in proportion on other scales) are
# highlighted as close results.
CLOSE_MARGIN = Decimal("0.25")


def _http(exc: Exception) -> HTTPException:
    if getattr(exc, "forbidden", False):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    conflict = getattr(exc, "conflict", False)
    code = status.HTTP_409_CONFLICT if conflict else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, str(exc))


# --- Rubrics -----------------------------------------------------------------------------------


@router.get("/categories/{category_id}/rubrics")
def list_rubrics(category_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> list[RubricOut]:
    _get_category(session, category_id)
    versions = session.scalars(
        select(RubricVersion)
        .where(RubricVersion.category_id == category_id)
        .order_by(RubricVersion.version.desc())
    )
    return [RubricOut.model_validate(r) for r in versions]


@router.post("/categories/{category_id}/rubrics", status_code=status.HTTP_201_CREATED)
def create_rubric(
    category_id: uuid.UUID, body: RubricIn, session: SessionDep, user: Coordinator
) -> RubricOut:
    """Create a draft rubric version: a copy of an earlier version of this category's rubric,
    the illustrative template, or the given criteria. New versions rate on 0-3 unless asked."""
    _get_category(session, category_id)
    source = None
    weighting = body.weighting
    if body.copy_from:
        source = session.get(RubricVersion, body.copy_from)
        if source is None or source.category_id != category_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rubric in this category")
        criteria, weighting, notes = rubrics.copy_of(source)
    elif body.use_template:
        criteria, notes = rubrics.DRAFT_TEMPLATE, rubrics.TEMPLATE_NOTE
    else:
        criteria, notes = [c.model_dump() for c in body.criteria], body.notes
    try:
        rubric = rubrics.create_draft(
            session,
            category_id,
            user,
            criteria,
            weighting=weighting,
            notes=notes,
            scale=body.scale or RatingScale.ZERO_TO_THREE,
            copied_from=source,
        )
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()
    session.refresh(rubric)
    return RubricOut.model_validate(rubric)


def _get_rubric(session: Session, rubric_id: uuid.UUID) -> RubricVersion:
    rubric = session.get(RubricVersion, rubric_id, with_for_update=True)
    if rubric is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rubric not found")
    return rubric


@router.put("/rubrics/{rubric_id}")
def update_rubric(
    rubric_id: uuid.UUID, body: RubricIn, session: SessionDep, user: Coordinator
) -> RubricOut:
    """Edit a draft rubric, or (administrators only) amend an approved one."""
    rubric = _get_rubric(session, rubric_id)
    try:
        rubrics.update_rubric(
            session,
            rubric,
            user,
            [c.model_dump() for c in body.criteria],
            weighting=body.weighting,
            notes=body.notes,
            scale=body.scale,
        )
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()
    session.refresh(rubric)
    return RubricOut.model_validate(rubric)


@router.delete("/rubrics/{rubric_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_rubric(rubric_id: uuid.UUID, session: SessionDep, user: Coordinator) -> None:
    """Discard a draft nobody has used."""
    rubric = _get_rubric(session, rubric_id)
    try:
        rubrics.discard_draft(session, rubric, user)
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()


@router.post("/rubrics/{rubric_id}/approve")
def approve_rubric(rubric_id: uuid.UUID, session: SessionDep, user: RubricApprover) -> RubricOut:
    rubric = _get_rubric(session, rubric_id)
    try:
        rubrics.approve(session, rubric, user)
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()
    return RubricOut.model_validate(rubric)


@router.post("/rubrics/{rubric_id}/freeze")
def freeze_rubric(rubric_id: uuid.UUID, session: SessionDep, user: RubricApprover) -> RubricOut:
    rubric = _get_rubric(session, rubric_id)
    try:
        rubrics.freeze(session, rubric, user)
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()
    return RubricOut.model_validate(rubric)


@router.post("/rubrics/{rubric_id}/unfreeze")
def unfreeze_rubric(
    rubric_id: uuid.UUID, body: UnfreezeIn, session: SessionDep, user: Admin
) -> RubricOut:
    """Administrators only: return a frozen rubric to approved so it can be amended."""
    rubric = _get_rubric(session, rubric_id)
    try:
        rubrics.unfreeze(session, rubric, user, body.reason)
    except rubrics.RubricError as exc:
        raise _http(exc) from exc
    session.commit()
    return RubricOut.model_validate(rubric)


# --- Assessment runs ---------------------------------------------------------------------------


def _latest_run(session: Session, category_id: uuid.UUID) -> AssessmentRun | None:
    return session.scalar(
        select(AssessmentRun)
        .where(AssessmentRun.category_id == category_id)
        .order_by(AssessmentRun.created_at.desc())
        .limit(1)
    )


@router.post("/assessments", status_code=status.HTTP_202_ACCEPTED)
def start_assessment(
    body: AssessmentRunIn,
    session: SessionDep,
    settings: SettingsDep,
    user: Coordinator,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> AssessmentRunOut:
    """Queue assessment of every finalist in a category against the current rubric."""
    _get_category(session, body.category_id)
    if idempotency_key:
        previous = session.scalar(
            select(AssessmentRun).where(AssessmentRun.idempotency_key == idempotency_key)
        )
        if previous is not None:
            if previous.category_id != body.category_id:
                raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key already used")
            return AssessmentRunOut.model_validate(previous)

    state = provider_status(settings)
    if body.mode == "ai" and not state["enabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, state["reason"])
    rubric = rubrics.current_rubric(session, body.category_id)
    if rubric is None or not rubric.criteria:
        raise HTTPException(status.HTTP_409_CONFLICT, "create a rubric for this category first")
    if not session.scalar(
        select(FinalistCategory.id).where(FinalistCategory.category_id == body.category_id).limit(1)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "this category has no finalists")

    human = body.mode == "human"
    run = AssessmentRun(
        id=uuid.uuid4(),
        category_id=body.category_id,
        rubric_version_id=rubric.id,
        rubric_status=rubric.status,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        llm_model="human" if human else settings.llm_model,
        prompt_version="none" if human else ASSESSMENT_PROMPT_VERSION,
        embedding_model="none" if human else settings.embedding_model,
        requested_by=user.id,
    )
    if human:  # nothing for the worker to do: reviewers enter each rating
        run.status = JobStatus.SUCCEEDED
        run.completed_at = datetime.now(UTC)
        run.skipped = [{"finalist": "all", "reason": "human-only assessment"}]
    session.add(run)
    audit.record(
        session,
        user,
        "assessment.requested",
        "assessment_run",
        run.id,
        rubric_version=rubric.version,
        rubric_status=rubric.status.value,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "an assessment is already queued or running for this category"
        ) from exc
    session.refresh(run)
    return AssessmentRunOut.model_validate(run)


@router.get("/assessments/{run_id}")
def get_assessment(run_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> AssessmentRunOut:
    run = session.get(AssessmentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assessment run not found")
    return AssessmentRunOut.model_validate(run)


def _assessment_out(session: Session, a: CriterionAssessment) -> CriterionAssessmentOut:
    criterion = session.get_one(RubricCriterion, a.criterion_id)
    chunk_sources = {
        row.id: row
        for row in session.execute(
            text(
                "SELECT c.id, c.document_id, c.page_number, d.filename FROM document_chunks c "
                "JOIN documents d ON d.id = c.document_id WHERE c.id = ANY(:ids)"
            ),
            {"ids": [c.chunk_id for c in a.citations if c.chunk_id]},
        )
    }
    citations = []
    for c in a.citations:
        if c.chunk_id:
            src = chunk_sources.get(c.chunk_id)
            citations.append(
                AssessmentCitationOut(
                    relation=c.relation.value,
                    quote=c.quoted_span,
                    document_id=src.document_id if src else None,
                    filename=src.filename if src else None,
                    page=src.page_number if src else None,
                )
            )
        else:
            public = session.get(ExternalSource, c.external_source_id)
            citations.append(
                AssessmentCitationOut(
                    relation=c.relation.value,
                    quote=c.quoted_span,
                    url=public.url if public else None,
                    publisher=public.publisher if public else None,
                )
            )
    reviewer = session.get(User, a.reviewed_by) if a.reviewed_by else None
    return CriterionAssessmentOut(
        id=a.id,
        criterion_id=criterion.id,
        criterion_code=criterion.code,
        criterion_name=criterion.name,
        rating=a.rating,
        reviewer_rating=a.reviewer_rating,
        effective_rating=a.effective_rating,
        rationale=a.rationale,
        limitations=a.limitations,
        sufficiency=a.sufficiency.value,
        flags=a.verification_flags,
        review_required=a.review_required,
        status=a.status,
        source=a.source,
        review_notes=a.review_notes,
        reviewed_by=reviewer.display_name if reviewer else None,
        reviewed_at=a.reviewed_at,
        citations=citations,
        discarded_citations=(a.raw_output or {}).get("discarded_citations", []),
    )


@router.get("/categories/{category_id}/finalists/{finalist_id}/assessments")
def candidate_assessments(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
) -> CandidateAssessmentsOut:
    """The candidate's criterion ratings from the category's latest assessment run."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    run = _latest_run(session, category_id)
    if is_blinded(session, user, category_id):
        return CandidateAssessmentsOut(
            assessment=provider_status(settings),
            run=None,
            rubric=None,
            items=[],
            hidden_for_calibration=True,
        )
    rubric = session.get(RubricVersion, run.rubric_version_id) if run else None
    items = []
    if run:
        position = {c.id: c.position for c in rubric.criteria}
        assessments = session.scalars(
            select(CriterionAssessment).where(
                CriterionAssessment.run_id == run.id,
                CriterionAssessment.finalist_category_id == fc.id,
            )
        ).all()
        items = [
            _assessment_out(session, a)
            for a in sorted(assessments, key=lambda a: position.get(a.criterion_id, 0))
        ]
    return CandidateAssessmentsOut(
        assessment=provider_status(settings),
        run=AssessmentRunOut.model_validate(run) if run else None,
        rubric=RubricOut.model_validate(rubric) if rubric else None,
        items=items,
    )


@router.post("/criterion-assessments/{assessment_id}/review")
def review(
    assessment_id: uuid.UUID, body: AssessmentReviewIn, session: SessionDep, user: Reviewer
) -> CriterionAssessmentOut:
    assessment = session.get(CriterionAssessment, assessment_id, with_for_update=True)
    if assessment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assessment not found")
    if is_blinded(session, user, session.get(AssessmentRun, assessment.run_id).category_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLINDED)
    try:
        review_assessment(
            session,
            assessment,
            user,
            body.decision,
            adjusted_rating=body.adjusted_rating,
            notes=body.notes,
            scale=rubrics.run_scale(session, assessment.run_id),
        )
    except AssessmentReviewRejected as exc:
        raise _http(exc) from exc
    session.commit()
    session.refresh(assessment)
    return _assessment_out(session, assessment)


# --- Rankings ----------------------------------------------------------------------------------


def _ranking_out(session: Session, snapshot: RankingSnapshot) -> RankingOut:
    rubric = session.get(RubricVersion, snapshot.rubric_version_id)
    scale = scale_of(rubric.scale)
    names = {
        row.id: row
        for row in session.execute(
            select(
                FinalistCategory.id,
                Finalist.id.label("finalist_id"),
                Finalist.full_name,
                Finalist.organisation,
            )
            .join(Finalist, Finalist.id == FinalistCategory.finalist_id)
            .where(FinalistCategory.id.in_([e.finalist_category_id for e in snapshot.entries]))
        )
    }
    entries = sorted(
        snapshot.entries,
        key=lambda e: (e.rank is None, e.rank or 0, names[e.finalist_category_id].full_name),
    )
    out = [
        RankingEntryOut(
            finalist_id=names[e.finalist_category_id].finalist_id,
            finalist_category_id=e.finalist_category_id,
            full_name=names[e.finalist_category_id].full_name,
            organisation=names[e.finalist_category_id].organisation,
            score=str(e.score) if e.score is not None else None,
            score_display=display(e.score),
            points_display=display_points(e.score, scale),
            rank=e.rank,
            is_tie=e.is_tie,
            criteria_assessed=e.criteria_assessed,
            criteria_total=e.criteria_total,
            missing_criteria=e.missing_criteria,
            unreviewed_criteria=e.unreviewed_criteria,
            ratings=e.criterion_ratings,
        )
        for e in entries
    ]
    scored = [e for e in entries if e.score is not None]
    close = [
        {
            "higher": names[a.finalist_category_id].full_name,
            "lower": names[b.finalist_category_id].full_name,
            "difference": display(a.score - b.score),
        }
        for a, b in zip(scored, scored[1:], strict=False)
        if a.score - b.score <= CLOSE_MARGIN * (scale.high - scale.low) / 4
    ]
    return RankingOut(
        id=snapshot.id,
        run_id=snapshot.run_id,
        created_at=snapshot.created_at,
        rubric_version=rubric.version,
        rubric_status=snapshot.rubric_status,
        production=snapshot.rubric_status == RubricStatus.FROZEN,
        weighting=snapshot.weighting,
        scale=scale.key,
        calculation_version=snapshot.calculation_version,
        is_provisional=snapshot.is_provisional,
        criteria=[CriterionOut.model_validate(c) for c in rubric.criteria],
        entries=out,
        close_results=close,
        disclaimer=DISCLAIMER,
    )


@router.post("/rankings", status_code=status.HTTP_201_CREATED)
def calculate_ranking(body: RankingIn, session: SessionDep, user: Coordinator) -> RankingOut:
    """Calculate a provisional ranking from a completed assessment run (latest by default)."""
    _get_category(session, body.category_id)
    if is_blinded(session, user, body.category_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLINDED)
    run = (
        session.get(AssessmentRun, body.run_id)
        if body.run_id
        else _latest_run(session, body.category_id)
    )
    if run is None or run.category_id != body.category_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no assessment run for this category")
    try:
        snapshot = create_snapshot(session, run, user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    session.refresh(snapshot)
    return _ranking_out(session, snapshot)


@router.get("/categories/{category_id}/rankings")
def category_rankings(
    category_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: CurrentUser
) -> CategoryRankingsOut:
    _get_category(session, category_id)
    snapshot = session.scalar(
        select(RankingSnapshot)
        .where(RankingSnapshot.category_id == category_id)
        .order_by(RankingSnapshot.created_at.desc())
        .limit(1)
    )
    rubric = rubrics.current_rubric(session, category_id)
    working = rubrics.working_rubric(session, category_id)
    run = _latest_run(session, category_id)
    hidden = is_blinded(session, user, category_id)
    study = latest_result(session, category_id)
    return CategoryRankingsOut(
        assessment=provider_status(settings),
        rubric=RubricOut.model_validate(rubric) if rubric else None,
        working_rubric=RubricOut.model_validate(working) if working else None,
        latest_run=AssessmentRunOut.model_validate(run) if run else None,
        ranking=_ranking_out(session, snapshot) if snapshot and not hidden else None,
        hidden_for_calibration=hidden,
        calibration=None
        if study is None
        else {
            "id": str(study.id),
            "name": study.name,
            "outcome": study.outcome,
            "run_id": str(study.run_id),
            "closed_at": study.closed_at.isoformat(),
            "reasons": (study.results or {}).get("reasons", []),
        },
    )


@router.post("/criterion-assessments/human", status_code=status.HTTP_201_CREATED)
def human_rating(
    body: HumanRatingIn, session: SessionDep, user: Reviewer
) -> CriterionAssessmentOut:
    """A rating entered by a reviewer, for a finalist the AI did not rate (no AI consent, or a
    human-only run). It counts in the ranking like an approved AI rating."""
    run = session.get(AssessmentRun, body.run_id)
    if run is None or run.status != JobStatus.SUCCEEDED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no completed assessment run")
    if is_blinded(session, user, run.category_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLINDED)
    fc = _get_finalist_category(session, run.category_id, body.finalist_id)
    rubric = session.get(RubricVersion, run.rubric_version_id)
    if body.criterion_id not in {c.id for c in rubric.criteria}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "criterion not in this run's rubric")
    exists_already = session.scalar(
        select(CriterionAssessment.id).where(
            CriterionAssessment.run_id == run.id,
            CriterionAssessment.finalist_category_id == fc.id,
            CriterionAssessment.criterion_id == body.criterion_id,
        )
    )
    if exists_already:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this criterion is already rated; review or adjust that rating",
        )
    scale = scale_of(rubric.scale)
    if not scale.contains(body.rating):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ratings are whole numbers from {scale.low} to {scale.high}",
        )
    now = datetime.now(UTC)
    assessment = CriterionAssessment(
        run_id=run.id,
        finalist_category_id=fc.id,
        criterion_id=body.criterion_id,
        rating=body.rating,
        rationale=body.rationale.strip(),
        sufficiency=body.sufficiency,
        verification_flags=["human_assessment"],
        review_required=False,
        status=AssessmentStatus.APPROVED,
        reviewed_by=user.id,
        reviewed_at=now,
        raw_output={"source": "human"},
        source="human",
    )
    session.add(assessment)
    session.flush()
    audit.record(
        session,
        user,
        "assessment.human_rating",
        "criterion_assessment",
        assessment.id,
        rating=body.rating,
    )
    session.commit()
    session.refresh(assessment)
    return _assessment_out(session, assessment)
