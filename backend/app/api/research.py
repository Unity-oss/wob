"""External research endpoints (FR-13)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.claims import Reviewer
from app.api.knowledge_base import SessionDep, _get_finalist_category
from app.config import Settings, get_settings
from app.models import Claim, ExternalSource, ResearchRun, ReviewOutcome, User
from app.schemas.research import ResearchOut, ResearchRunOut, SourceOut, SourceReviewIn
from app.security.auth import CurrentUser
from app.services import audit, consent
from app.services.research.llm import research_settings, research_status
from app.services.rubrics import current_rubric
from app.services.verification import ReviewRejected, review_external_source
from app.workers.research import new_research_run

router = APIRouter(prefix="/api/v1", tags=["external research"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _source_out(session: Session, source: ExternalSource) -> SourceOut:
    claim = session.get(Claim, source.claim_id) if source.claim_id else None
    reviewer = session.get(User, source.reviewed_by) if source.reviewed_by else None
    return SourceOut(
        id=source.id,
        claim_id=source.claim_id,
        claim_statement=claim.wording if claim else None,
        claim_current=bool(claim and claim.superseded_at is None),
        criterion_code=source.criterion_code,
        criterion_name=source.criterion_name,
        relation=source.relation,
        publisher=source.publisher,
        url=source.url,
        title=source.title,
        published_at=source.published_at,
        retrieved_at=source.retrieved_at,
        quote=source.quote,
        evidence_passage=source.evidence_passage,
        source_type=source.source_type,
        identity_confidence=source.identity_confidence,
        identity_notes=source.identity_notes,
        identity_confirmed=source.identity_confirmed,
        credibility=source.credibility,
        reviewer_outcome=source.reviewer_outcome,
        auto_accepted=source.auto_accepted,
        reviewed_by=reviewer.display_name if reviewer else None,
        reviewed_at=source.reviewed_at,
        review_notes=source.review_notes,
    )


def _latest_run(session: Session, finalist_category_id: uuid.UUID) -> ResearchRun | None:
    return session.scalar(
        select(ResearchRun)
        .where(ResearchRun.finalist_category_id == finalist_category_id)
        .order_by(ResearchRun.created_at.desc())
        .limit(1)
    )


@router.get("/categories/{category_id}/finalists/{finalist_id}/research")
def get_research(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    _: CurrentUser,
) -> ResearchOut:
    """Public-source findings: pending ones from the latest research, plus every reviewed one."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    sources = session.scalars(
        select(ExternalSource)
        .where(
            ExternalSource.finalist_category_id == fc.id,
            or_(
                ExternalSource.superseded_at.is_(None),
                ExternalSource.reviewer_outcome != ReviewOutcome.PENDING,
            ),
        )
        .order_by(ExternalSource.reviewer_outcome, ExternalSource.created_at)
    )
    run = _latest_run(session, fc.id)
    return ResearchOut(
        research=research_status(settings),
        latest_run=ResearchRunOut.model_validate(run) if run else None,
        findings=[_source_out(session, s) for s in sources],
    )


@router.post(
    "/categories/{category_id}/finalists/{finalist_id}/research",
    status_code=status.HTTP_202_ACCEPTED,
)
def start_research(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: Reviewer,
) -> ResearchRunOut:
    """Queue a search of credible public sources for the candidate's current claims and for each
    criterion of the category's rubric."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    state = research_status(settings)
    if not state["enabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, state["reason"])
    if not consent.has(session, fc.id, consent.Purpose.WEB_RESEARCH):
        raise HTTPException(
            status.HTTP_409_CONFLICT, consent.MESSAGES[consent.Purpose.WEB_RESEARCH]
        )
    claims = session.scalar(
        select(func.count(Claim.id)).where(
            Claim.finalist_category_id == fc.id, Claim.superseded_at.is_(None)
        )
    )
    if not claims and current_rubric(session, fc.category_id) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "nothing to research: extract claims, or create a rubric so research can look for "
            "public evidence on each criterion",
        )
    model = research_settings(settings).llm_model
    run = new_research_run(session, fc.id, model, user.id)
    audit.record(
        session, user, "research.requested", "research_run", run.id, finalist_category_id=str(fc.id)
    )
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "research is already queued or running for this candidate"
        ) from exc
    session.refresh(run)
    return ResearchRunOut.model_validate(run)


@router.get("/research-runs/{run_id}")
def get_research_run(run_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> ResearchRunOut:
    run = session.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found")
    return ResearchRunOut.model_validate(run)


@router.post("/external-sources/{source_id}/review")
def review_source(
    source_id: uuid.UUID, body: SourceReviewIn, session: SessionDep, user: Reviewer
) -> SourceOut:
    """Accept a finding as evidence for its claim or criterion, or reject it (including one that
    was accepted automatically)."""
    source = session.get(ExternalSource, source_id, with_for_update=True)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    try:
        review_external_source(
            session,
            source,
            user,
            body.outcome,
            credibility=body.credibility,
            identity_confirmed=body.identity_confirmed,
            notes=body.notes,
        )
    except ReviewRejected as exc:
        code = status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, str(exc)) from exc
    session.commit()
    session.refresh(source)
    return _source_out(session, source)
