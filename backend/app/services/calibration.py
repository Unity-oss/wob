"""Calibration studies (FR-15): blind human benchmark ratings compared with the AI's.

Blinding: while a study is open, its raters who have not yet submitted cannot see AI ratings,
rankings or reports for that category, so their ratings are independent of the AI's.
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    AssessmentRun,
    CalibrationItem,
    CalibrationRater,
    CalibrationRating,
    CalibrationStudy,
    CriterionAssessment,
    JobStatus,
    RubricCriterion,
    RubricVersion,
    User,
)
from app.security.permissions import Permission, roles_with
from app.services import audit
from app.services.calibration_metrics import Agreement, Thresholds, agreement, verdict
from app.services.scales import scale_of


class CalibrationError(ValueError):
    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


def is_blinded(session: Session, user: User, category_id: uuid.UUID) -> bool:
    """True if the user rates in an open study for this category and hasn't submitted yet."""
    return bool(
        session.scalar(
            select(
                exists().where(
                    CalibrationRater.user_id == user.id,
                    CalibrationRater.completed_at.is_(None),
                    CalibrationRater.study_id == CalibrationStudy.id,
                    CalibrationStudy.category_id == category_id,
                    CalibrationStudy.status == "open",
                )
            )
        )
    )


def create_study(
    session: Session,
    *,
    category_id: uuid.UUID,
    name: str,
    rater_ids: list[uuid.UUID],
    creator: User,
    finalist_category_ids: list[uuid.UUID] | None = None,
    run_id: uuid.UUID | None = None,
    thresholds: Thresholds | None = None,
) -> CalibrationStudy:
    category_id = uuid.UUID(str(category_id))
    rater_ids = [uuid.UUID(str(r)) for r in rater_ids]
    run = (
        session.get(AssessmentRun, run_id)
        if run_id
        else session.scalar(
            select(AssessmentRun)
            .where(
                AssessmentRun.category_id == category_id,
                AssessmentRun.status == JobStatus.SUCCEEDED,
            )
            .order_by(AssessmentRun.created_at.desc())
            .limit(1)
        )
    )
    if run is None or run.category_id != category_id or run.status != JobStatus.SUCCEEDED:
        raise CalibrationError(
            "a completed assessment run is needed to calibrate against", conflict=True
        )
    raters = session.scalars(select(User).where(User.id.in_(rater_ids))).all()
    if len(raters) != len(set(rater_ids)) or not raters:
        raise CalibrationError("choose at least one rater; every rater must be an existing user")
    rater_roles = roles_with(session, Permission.RATE_CALIBRATION)
    for rater in raters:
        if not rater.is_active or rater.role not in rater_roles:
            raise CalibrationError(
                f"{rater.display_name} cannot rate: needs an active account in a user group "
                "allowed to rate in calibration studies"
            )

    assessed = session.execute(
        select(CriterionAssessment.finalist_category_id, CriterionAssessment.criterion_id)
        .join(RubricCriterion, RubricCriterion.id == CriterionAssessment.criterion_id)
        .where(CriterionAssessment.run_id == run.id, CriterionAssessment.source == "ai")
        .order_by(CriterionAssessment.finalist_category_id, RubricCriterion.position)
    ).all()
    if finalist_category_ids is not None:
        assessed = [row for row in assessed if row[0] in set(finalist_category_ids)]
    if not assessed:
        raise CalibrationError("the run has no AI ratings for the chosen finalists")

    study = CalibrationStudy(
        id=uuid.uuid4(),
        category_id=category_id,
        run_id=run.id,
        rubric_version_id=run.rubric_version_id,
        name=name,
        created_by=creator.id,
        thresholds=asdict(thresholds or Thresholds()),
    )
    session.add(study)
    session.flush()
    session.add_all(CalibrationRater(study_id=study.id, user_id=r.id) for r in raters)
    session.add_all(
        CalibrationItem(study_id=study.id, finalist_category_id=fc, criterion_id=c, position=n)
        for n, (fc, c) in enumerate(assessed)
    )
    audit.record(
        session,
        creator,
        "calibration.created",
        "calibration_study",
        study.id,
        run_id=str(run.id),
        raters=[str(r.id) for r in raters],
        items=len(assessed),
    )
    return study


def _rater(session: Session, study: CalibrationStudy, user: User) -> CalibrationRater:
    rater = session.get(CalibrationRater, (study.id, user.id))
    if rater is None:
        raise CalibrationError("you are not a rater in this study")
    if study.status != "open":
        raise CalibrationError(f"the study is {study.status}", conflict=True)
    if rater.completed_at:
        raise CalibrationError("you have already submitted your ratings", conflict=True)
    return rater


def rate(
    session: Session,
    study: CalibrationStudy,
    user: User,
    item_id: uuid.UUID,
    rating: int,
    notes: str | None,
) -> CalibrationRating:
    _rater(session, study, user)
    scale = scale_of(session.get_one(RubricVersion, study.rubric_version_id).scale)
    if not scale.contains(rating):
        raise CalibrationError(f"ratings are whole numbers from {scale.low} to {scale.high}")
    item = session.get(CalibrationItem, item_id)
    if item is None or item.study_id != study.id:
        raise CalibrationError("item not in this study")
    existing = session.scalar(
        select(CalibrationRating).where(
            CalibrationRating.item_id == item_id, CalibrationRating.rater_id == user.id
        )
    )
    if existing:
        existing.rating, existing.notes = rating, notes
        existing.updated_at = datetime.now(UTC)
        return existing
    record = CalibrationRating(item_id=item_id, rater_id=user.id, rating=rating, notes=notes)
    session.add(record)
    return record


def submit(session: Session, study: CalibrationStudy, user: User, attest_blind: bool) -> None:
    rater = _rater(session, study, user)
    if not attest_blind:
        raise CalibrationError("confirm that you rated without seeing the AI's ratings")
    unrated = session.scalars(
        select(CalibrationItem.id).where(
            CalibrationItem.study_id == study.id,
            ~exists().where(
                CalibrationRating.item_id == CalibrationItem.id,
                CalibrationRating.rater_id == user.id,
            ),
        )
    ).all()
    if unrated:
        raise CalibrationError(f"rate every item first ({len(unrated)} left)")
    rater.completed_at = datetime.now(UTC)
    rater.attested_blind = True
    audit.record(session, user, "calibration.submitted", "calibration_study", study.id)


def compute(session: Session, study: CalibrationStudy) -> dict:
    """Agreement overall and per criterion, from completed raters only."""
    items = session.scalars(
        select(CalibrationItem)
        .where(CalibrationItem.study_id == study.id)
        .order_by(CalibrationItem.position)
    ).all()
    ai = {
        (a.finalist_category_id, a.criterion_id): a.rating
        for a in session.scalars(
            select(CriterionAssessment).where(CriterionAssessment.run_id == study.run_id)
        )
    }
    completed = session.scalars(
        select(CalibrationRater.user_id).where(
            CalibrationRater.study_id == study.id, CalibrationRater.completed_at.is_not(None)
        )
    ).all()
    ratings = {
        (r.item_id, r.rater_id): r.rating
        for r in session.scalars(
            select(CalibrationRating).where(
                CalibrationRating.item_id.in_([i.id for i in items]),
                CalibrationRating.rater_id.in_(completed),
            )
        )
    }
    rubric = session.get_one(RubricVersion, study.rubric_version_id)
    criteria = {c.id: c.name for c in rubric.criteria}
    levels = range(scale_of(rubric.scale).low, scale_of(rubric.scale).high + 1)
    thresholds = Thresholds(**study.thresholds)

    def measure(subset: list[CalibrationItem]) -> Agreement:
        return agreement(
            [ai[(i.finalist_category_id, i.criterion_id)] for i in subset],
            {str(u): [ratings.get((i.id, u)) for i in subset] for u in completed},
            levels,
        )

    overall = measure(items)
    by_criterion = {
        name: measure([i for i in items if i.criterion_id == cid])
        for cid, name in criteria.items()
        if any(i.criterion_id == cid for i in items)
    }
    outcome, reasons = verdict(overall, by_criterion, thresholds)
    finalists = len({i.finalist_category_id for i in items})
    return {
        "outcome": outcome,
        "reasons": reasons,
        "raters_completed": len(completed),
        "finalists": finalists,
        "overall": asdict(overall),
        "by_criterion": {name: asdict(a) for name, a in by_criterion.items()},
        "thresholds": asdict(thresholds),
        "computed_at": datetime.now(UTC).isoformat(),
    }


def close(session: Session, study: CalibrationStudy, user: User) -> dict:
    if study.status != "open":
        raise CalibrationError(f"the study is already {study.status}", conflict=True)
    results = compute(session, study)
    if results["raters_completed"] == 0:
        raise CalibrationError("no rater has submitted yet", conflict=True)
    study.results = results
    study.outcome = results["outcome"]
    study.status = "closed"
    study.closed_at = datetime.now(UTC)
    audit.record(
        session,
        user,
        "calibration.closed",
        "calibration_study",
        study.id,
        outcome=results["outcome"],
        reasons=results["reasons"],
    )
    return results


def latest_result(session: Session, category_id: uuid.UUID) -> CalibrationStudy | None:
    return session.scalar(
        select(CalibrationStudy)
        .where(CalibrationStudy.category_id == category_id, CalibrationStudy.status == "closed")
        .order_by(CalibrationStudy.closed_at.desc())
        .limit(1)
    )
