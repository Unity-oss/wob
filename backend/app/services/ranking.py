"""Deterministic provisional ranking (spec §7.2, FR-09).

    equal weighting:    score = sum(ratings) / number of criteria
    weighted scoring:   score = sum(weight × rating) / sum(weights)

Scores are exact decimals and ranks use the full-precision score; display rounds to two
decimals, and to a score out of 100 for a points view (×20 on the 1-5 scale, ×100/3 on 0-3).
A finalist missing any criterion gets no score rather than an average of the rest. Equal
scores share a rank and are flagged as provisional ties: the committee's tie-breaking procedure
is not yet approved.
"""

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AssessmentRun,
    AssessmentStatus,
    CriterionAssessment,
    FinalistCategory,
    JobStatus,
    RankingEntry,
    RankingSnapshot,
    RubricVersion,
    User,
    WeightingMode,
)
from app.services import audit
from app.services.scales import SCALES, RatingScale, Scale, scale_of

CALCULATION_VERSION = "rank-v1"
TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class CriterionSpec:
    id: uuid.UUID
    code: str
    weight: Decimal | None = None


@dataclass
class CandidateRatings:
    finalist_category_id: uuid.UUID
    ratings: dict[uuid.UUID, int | None]  # None: not assessed, or assessment rejected
    unreviewed: list[str] = field(default_factory=list)


@dataclass
class RankedEntry:
    finalist_category_id: uuid.UUID
    score: Decimal | None
    rank: int | None
    is_tie: bool
    criteria_assessed: int
    criteria_total: int
    missing: list[str]
    unreviewed: list[str]
    ratings: dict[str, int | None]


def calculate_score(
    ratings: dict[uuid.UUID, int | None],
    criteria: list[CriterionSpec],
    weighted: bool,
    scale: Scale = SCALES[RatingScale.ONE_TO_FIVE],
) -> Decimal | None:
    values = [ratings.get(c.id) for c in criteria]
    if not criteria or any(v is None for v in values):
        return None
    if any(not scale.contains(v) for v in values):  # type: ignore[arg-type]
        raise ValueError(f"ratings must be integers from {scale.low} to {scale.high}")
    if not weighted:
        return Decimal(sum(values)) / Decimal(len(values))  # type: ignore[arg-type]
    if any(c.weight is None or c.weight <= 0 for c in criteria):
        raise ValueError("weighted scoring needs a positive weight for every criterion")
    total = sum(c.weight * v for c, v in zip(criteria, values, strict=True))  # type: ignore[operator]
    return total / sum(c.weight for c in criteria)  # type: ignore[misc]


def rank(
    candidates: list[CandidateRatings],
    criteria: list[CriterionSpec],
    weighted: bool,
    scale: Scale = SCALES[RatingScale.ONE_TO_FIVE],
) -> list[RankedEntry]:
    entries = []
    for candidate in candidates:
        score = calculate_score(candidate.ratings, criteria, weighted, scale)
        entries.append(
            RankedEntry(
                finalist_category_id=candidate.finalist_category_id,
                score=score,
                rank=None,
                is_tie=False,
                criteria_assessed=sum(candidate.ratings.get(c.id) is not None for c in criteria),
                criteria_total=len(criteria),
                missing=[c.code for c in criteria if candidate.ratings.get(c.id) is None],
                unreviewed=list(candidate.unreviewed),
                ratings={c.code: candidate.ratings.get(c.id) for c in criteria},
            )
        )
    scored = [e for e in entries if e.score is not None]
    for entry in scored:
        # Competition ranking: 1 + number of strictly higher scores (1, 1, 3, ...).
        entry.rank = 1 + sum(other.score > entry.score for other in scored)  # type: ignore[operator]
        entry.is_tie = sum(other.score == entry.score for other in scored) > 1
    # Deterministic order: by rank, then unscored last, then by id.
    entries.sort(key=lambda e: (e.rank is None, e.rank or 0, str(e.finalist_category_id)))
    return entries


def display(score: Decimal | None) -> str | None:
    return None if score is None else str(score.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def display_points(
    score: Decimal | None, scale: Scale = SCALES[RatingScale.ONE_TO_FIVE]
) -> str | None:
    if score is None:
        return None
    return str(scale.points(score).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def create_snapshot(session: Session, run: AssessmentRun, user: User) -> RankingSnapshot:
    """Rank a category from a completed assessment run and store an immutable snapshot.

    Ratings used: the reviewer-adjusted rating if any, else the AI's. Rejected assessments count
    as missing (re-assess), and unreviewed ones are listed so nobody mistakes them for checked.
    """
    if run.status != JobStatus.SUCCEEDED:
        raise ValueError("the assessment run has not completed")
    rubric = session.get(RubricVersion, run.rubric_version_id)
    criteria = [CriterionSpec(c.id, c.code, c.weight) for c in rubric.criteria]
    by_id = {c.id: c for c in criteria}
    assessments = session.scalars(
        select(CriterionAssessment).where(CriterionAssessment.run_id == run.id)
    ).all()
    finalists = session.scalars(
        select(FinalistCategory.id).where(FinalistCategory.category_id == run.category_id)
    ).all()

    candidates = {fc: CandidateRatings(fc, {c.id: None for c in criteria}) for fc in finalists}
    for a in assessments:
        candidate = candidates.get(a.finalist_category_id)
        if candidate is None or a.criterion_id not in by_id:
            continue
        if a.status == AssessmentStatus.REJECTED:
            continue
        candidate.ratings[a.criterion_id] = a.effective_rating
        if a.status == AssessmentStatus.PROPOSED:
            candidate.unreviewed.append(by_id[a.criterion_id].code)

    weighted = rubric.weighting == WeightingMode.WEIGHTED
    ranked = rank(list(candidates.values()), criteria, weighted, scale_of(rubric.scale))
    snapshot = RankingSnapshot(
        category_id=run.category_id,
        run_id=run.id,
        rubric_version_id=rubric.id,
        rubric_status=rubric.status,
        weighting=rubric.weighting,
        calculation_version=CALCULATION_VERSION,
        is_provisional=True,
        created_by=user.id,
    )
    session.add(snapshot)
    session.flush()
    for entry in ranked:
        session.add(
            RankingEntry(
                snapshot_id=snapshot.id,
                finalist_category_id=entry.finalist_category_id,
                score=entry.score,
                rank=entry.rank,
                is_tie=entry.is_tie,
                criteria_assessed=entry.criteria_assessed,
                criteria_total=entry.criteria_total,
                missing_criteria=entry.missing,
                unreviewed_criteria=entry.unreviewed,
                criterion_ratings=entry.ratings,
            )
        )
    audit.record(
        session,
        user,
        "ranking.calculated",
        "ranking_snapshot",
        snapshot.id,
        run_id=str(run.id),
        rubric_status=rubric.status.value,
        scores={str(e.finalist_category_id): display(e.score) for e in ranked},
    )
    return snapshot
