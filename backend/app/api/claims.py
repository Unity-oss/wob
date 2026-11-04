"""Claim extraction (FR-06) and reviewer verification (FR-07) endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep, _get_finalist_category
from app.config import Settings, get_settings
from app.models import (
    Claim,
    ClaimExtractionRun,
    ClaimStatus,
    ContradictionResolution,
    Document,
    ExternalSource,
    ProcessingStatus,
    VerificationReview,
)
from app.schemas.claims import (
    ClaimEvidenceOut,
    ClaimOut,
    ClaimsOut,
    ExtractionRunOut,
    ExtractRequestOut,
    ResolutionIn,
    ResolutionOut,
    ReviewIn,
    ReviewOut,
)
from app.security.auth import Coordinator, CurrentUser, Reviewer
from app.services import audit, consent
from app.services.claims.llm import provider_status
from app.services.verification import (
    ReviewRejected,
    independent_sources,
    latest_reviews,
    review_claim,
)
from app.workers.claims import latest_run, new_run

router = APIRouter(prefix="/api/v1", tags=["claims"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _review_out(review: VerificationReview) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        reviewer=review.reviewer.display_name if review.reviewer else "Jury decision",
        reviewer_role=review.reviewer.role.value if review.reviewer else "automatic",
        previous_status=review.previous_status,
        decision=review.decision,
        notes=review.notes,
        sufficiency=review.sufficiency,
        credibility=review.credibility,
        carried_forward=review.carried_from is not None,
        created_at=review.created_at,
    )


def _run_out(session: Session, run: ClaimExtractionRun) -> ExtractionRunOut:
    out = ExtractionRunOut.model_validate(run)
    resolutions = session.scalars(
        select(ContradictionResolution)
        .where(ContradictionResolution.extraction_run_id == run.id)
        .order_by(ContradictionResolution.created_at)
    )
    out.contradiction_resolutions = {
        r.contradiction_index: ResolutionOut(
            resolution=r.resolution, resolved_by=r.resolver.display_name, created_at=r.created_at
        )
        for r in resolutions  # later rows overwrite earlier: the latest resolution wins
    }
    return out


def _claim_out(
    session: Session,
    claim: Claim,
    latest: VerificationReview | None = None,
    review_count: int = 0,
) -> ClaimOut:
    chunk_ids = [e.chunk_id for e in claim.evidence if e.chunk_id]
    sources = {
        row.id: row
        for row in session.execute(
            text(
                "SELECT c.id, c.document_id, c.page_number, d.filename, "
                "d.withdrawn_at IS NOT NULL AS withdrawn "
                "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(:ids)"
            ),
            {"ids": chunk_ids},
        )
    }
    external = {
        s.id: s
        for s in session.scalars(
            select(ExternalSource).where(
                ExternalSource.id.in_(
                    [e.external_source_id for e in claim.evidence if e.external_source_id]
                )
            )
        )
    }
    evidence = []
    for e in claim.evidence:
        if e.external_source_id:
            public = external[e.external_source_id]
            evidence.append(
                ClaimEvidenceOut(
                    relation=e.relation,
                    quote=e.quoted_span,
                    chunk_id=None,
                    document_id=None,
                    filename=None,
                    page=None,
                    document_withdrawn=False,
                    external_source_id=public.id,
                    url=public.url,
                    publisher=public.publisher,
                )
            )
            continue
        source = sources.get(e.chunk_id)
        evidence.append(
            ClaimEvidenceOut(
                relation=e.relation,
                quote=e.quoted_span,
                chunk_id=e.chunk_id,
                document_id=source.document_id if source else None,
                filename=source.filename if source else None,
                page=source.page_number if source else None,
                document_withdrawn=bool(source and source.withdrawn),
            )
        )
    supporting_docs = {
        ev.document_id for ev in evidence if ev.relation == "supports" and ev.document_id
    }
    return ClaimOut(
        id=claim.id,
        statement=claim.wording,
        claim_type=claim.claim_type,
        entity=claim.entity,
        role=claim.role,
        period_start=claim.period_start,
        period_start_precision=claim.period_start_precision,
        period_end=claim.period_end,
        period_end_precision=claim.period_end_precision,
        period_text=claim.period_text,
        quantitative_outcomes=claim.quantitative_outcomes,
        limitations=claim.limitations,
        status=claim.status,
        evidence=evidence,
        supporting_documents=len(supporting_docs),
        independent_sources=independent_sources(session, claim.id),
        latest_review=_review_out(latest) if latest else None,
        review_count=review_count,
    )


@router.get("/categories/{category_id}/finalists/{finalist_id}/claims")
def list_claims(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    _: CurrentUser,
) -> ClaimsOut:
    """Current (non-superseded) claims with their evidence, and the latest extraction run."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    claims = session.scalars(
        select(Claim)
        .where(Claim.finalist_category_id == fc.id, Claim.superseded_at.is_(None))
        .order_by(Claim.claim_type, Claim.period_start.desc().nulls_last(), Claim.wording)
    ).all()
    ids = [c.id for c in claims]
    latest = latest_reviews(session, ids)
    counts = dict(
        session.execute(
            select(VerificationReview.claim_id, func.count())
            .where(VerificationReview.claim_id.in_(ids))
            .group_by(VerificationReview.claim_id)
        ).all()
    )
    run = latest_run(session, fc.id)
    run_out = _run_out(session, run) if run else None
    by_status = {s.value: 0 for s in ClaimStatus}
    for c in claims:
        by_status[c.status.value] += 1
    unresolved = 0
    if run_out and run_out.status == "succeeded":
        unresolved = len(run_out.contradictions) - len(run_out.contradiction_resolutions)
    return ClaimsOut(
        extraction=provider_status(settings),
        verification={
            "total": len(claims),
            "reviewed": len(latest),
            "by_status": by_status,
            "unresolved_contradictions": unresolved,
        },
        latest_run=run_out,
        claims=[_claim_out(session, c, latest.get(c.id), counts.get(c.id, 0)) for c in claims],
    )


@router.post(
    "/categories/{category_id}/finalists/{finalist_id}/claims/extract",
    status_code=status.HTTP_202_ACCEPTED,
)
def extract_claims(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: Coordinator,
) -> ExtractRequestOut:
    """Queue claim extraction over the candidate's indexed evidence. Replaces current claims
    when it completes."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    provider = provider_status(settings)
    if not provider["enabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, provider["reason"])
    if not consent.has(session, fc.id, consent.Purpose.AI_PROCESSING):
        raise HTTPException(
            status.HTTP_409_CONFLICT, consent.MESSAGES[consent.Purpose.AI_PROCESSING]
        )

    indexed = session.scalar(
        text("SELECT count(*) FROM knowledge_base_chunks WHERE finalist_category_id = :fc"),
        {"fc": fc.id},
    )
    if not indexed:
        raise HTTPException(status.HTTP_409_CONFLICT, "this candidate has no indexed evidence yet")
    in_progress = session.scalar(
        select(func.count(Document.id)).where(
            Document.finalist_category_id == fc.id,
            Document.withdrawn_at.is_(None),
            Document.processing_status.not_in(
                [ProcessingStatus.INDEXED, ProcessingStatus.REJECTED, ProcessingStatus.FAILED]
            ),
        )
    )
    warnings = []
    if in_progress:
        warnings.append(
            f"{in_progress} document(s) are still being processed and won't be included; "
            "run extraction again once they are indexed"
        )

    model = settings.llm_model
    run = new_run(session, fc.id, model, user.id)
    audit.record(
        session,
        user,
        "claims.extraction_requested",
        "claim_extraction_run",
        run.id,
        finalist_category_id=str(fc.id),
    )
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an extraction is already queued or running for this candidate",
        ) from exc
    session.refresh(run)
    return ExtractRequestOut(run=_run_out(session, run), warnings=warnings)


@router.get("/claim-extraction-runs/{run_id}")
def get_run(run_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> ExtractionRunOut:
    run = session.get(ClaimExtractionRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "extraction run not found")
    return _run_out(session, run)


# --- Verification (FR-07) ----------------------------------------------------------------------


def _get_claim(session: Session, claim_id: uuid.UUID) -> Claim:
    claim = session.get(Claim, claim_id, with_for_update=True)
    if claim is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "claim not found")
    return claim


@router.post("/claims/{claim_id}/reviews", status_code=status.HTTP_201_CREATED)
def add_review(
    claim_id: uuid.UUID, body: ReviewIn, session: SessionDep, user: Reviewer
) -> ClaimOut:
    """Record a reviewer's decision on a claim; the claim takes the decided status."""
    claim = _get_claim(session, claim_id)
    try:
        review = review_claim(
            session,
            claim,
            user,
            body.decision,
            expected_status=body.expected_status,
            notes=body.notes,
            sufficiency=body.sufficiency,
            credibility=body.credibility,
        )
    except ReviewRejected as exc:
        code = status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, str(exc)) from exc
    session.commit()
    session.refresh(review)
    count = session.scalar(select(func.count()).where(VerificationReview.claim_id == claim.id))
    return _claim_out(session, claim, review, count or 0)


@router.get("/claims/{claim_id}/reviews")
def list_reviews(claim_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> list[ReviewOut]:
    if session.get(Claim, claim_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "claim not found")
    reviews = session.scalars(
        select(VerificationReview)
        .where(VerificationReview.claim_id == claim_id)
        .order_by(VerificationReview.created_at.desc())
    )
    return [_review_out(r) for r in reviews]


@router.post(
    "/claim-extraction-runs/{run_id}/contradictions/{index}/resolutions",
    status_code=status.HTTP_201_CREATED,
)
def resolve_contradiction(
    run_id: uuid.UUID, index: int, body: ResolutionIn, session: SessionDep, user: Reviewer
) -> ExtractionRunOut:
    """Record how a reviewer resolved a contradiction (spec §6: resolve manually)."""
    run = session.get(ClaimExtractionRun, run_id)
    if run is None or not 0 <= index < len(run.contradictions):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contradiction not found")
    session.add(
        ContradictionResolution(
            extraction_run_id=run.id,
            contradiction_index=index,
            resolution=body.resolution.strip(),
            resolved_by=user.id,
        )
    )
    audit.record(
        session,
        user,
        "contradiction.resolved",
        "claim_extraction_run",
        run.id,
        index=index,
        resolution=body.resolution.strip(),
    )
    session.commit()
    return _run_out(session, run)
