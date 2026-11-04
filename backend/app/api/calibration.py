"""Calibration studies (FR-15): blind benchmark ratings by committee raters vs AI ratings."""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep, _get_category
from app.models import (
    CalibrationItem,
    CalibrationRater,
    CalibrationRating,
    CalibrationStudy,
    Finalist,
    FinalistCategory,
    RubricCriterion,
    RubricVersion,
    User,
)
from app.security.auth import CurrentUser, StudyManager
from app.security.permissions import Permission, roles_with
from app.services import calibration
from app.services.calibration import CalibrationError
from app.services.scales import levels_out

router = APIRouter(prefix="/api/v1", tags=["calibration"])


def _http(exc: CalibrationError) -> HTTPException:
    code = status.HTTP_409_CONFLICT if exc.conflict else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, str(exc))


class StudyIn(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    rater_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    finalist_ids: list[uuid.UUID] | None = None


def _get_study(session: Session, study_id: uuid.UUID) -> CalibrationStudy:
    study = session.get(CalibrationStudy, study_id, with_for_update=True)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "calibration study not found")
    return study


def _summary(session: Session, study: CalibrationStudy, user: User) -> dict:
    raters = session.execute(
        select(CalibrationRater, User.display_name)
        .join(User, User.id == CalibrationRater.user_id)
        .where(CalibrationRater.study_id == study.id)
        .order_by(User.display_name)
    ).all()
    items = session.scalar(
        select(func.count(CalibrationItem.id)).where(CalibrationItem.study_id == study.id)
    )
    rated = dict(
        session.execute(
            select(CalibrationRating.rater_id, func.count())
            .join(CalibrationItem, CalibrationItem.id == CalibrationRating.item_id)
            .where(CalibrationItem.study_id == study.id)
            .group_by(CalibrationRating.rater_id)
        ).all()
    )
    me = next((r for r, _ in raters if r.user_id == user.id), None)
    return {
        "id": str(study.id),
        "category_id": str(study.category_id),
        "run_id": str(study.run_id),
        "name": study.name,
        "status": study.status,
        "outcome": study.outcome,
        "created_at": study.created_at.isoformat(),
        "closed_at": study.closed_at.isoformat() if study.closed_at else None,
        "items": items,
        "raters": [
            {
                "name": name,
                "rated": rated.get(r.user_id, 0),
                "submitted": r.completed_at is not None,
            }
            for r, name in raters
        ],
        "you": None
        if me is None
        else {
            "rated": rated.get(user.id, 0),
            "submitted": me.completed_at is not None,
        },
        "thresholds": study.thresholds,
    }


@router.post("/calibration-studies", status_code=status.HTTP_201_CREATED)
def create_study(body: StudyIn, session: SessionDep, user: StudyManager) -> dict:
    _get_category(session, body.category_id)
    fc_ids = None
    if body.finalist_ids is not None:
        fc_ids = list(
            session.scalars(
                select(FinalistCategory.id).where(
                    FinalistCategory.category_id == body.category_id,
                    FinalistCategory.finalist_id.in_(body.finalist_ids),
                )
            )
        )
    try:
        study = calibration.create_study(
            session,
            category_id=body.category_id,
            name=body.name,
            rater_ids=body.rater_ids,
            creator=user,
            finalist_category_ids=fc_ids,
        )
    except CalibrationError as exc:
        raise _http(exc) from exc
    session.commit()
    return _summary(session, study, user)


@router.get("/calibration/eligible-raters")
def eligible_raters(session: SessionDep, _: StudyManager) -> list[dict]:
    users = session.scalars(
        select(User)
        .where(
            User.is_active,
            User.role.in_(roles_with(session, Permission.RATE_CALIBRATION)),
        )
        .order_by(User.display_name)
    )
    return [{"id": str(u.id), "display_name": u.display_name, "role": u.role.value} for u in users]


@router.get("/categories/{category_id}/calibration-studies")
def list_studies(category_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> list[dict]:
    _get_category(session, category_id)
    studies = session.scalars(
        select(CalibrationStudy)
        .where(CalibrationStudy.category_id == category_id)
        .order_by(CalibrationStudy.created_at.desc())
    )
    return [_summary(session, s, user) for s in studies]


@router.get("/calibration-studies/{study_id}/items")
def my_items(study_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> dict:
    """The rater's worksheet: finalists and criteria to rate, with their own ratings only.
    AI ratings are never included."""
    study = _get_study(session, study_id)
    if session.get(CalibrationRater, (study.id, user.id)) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a rater in this study")
    rows = session.execute(
        select(CalibrationItem, Finalist.id, Finalist.full_name, RubricCriterion)
        .join(FinalistCategory, FinalistCategory.id == CalibrationItem.finalist_category_id)
        .join(Finalist, Finalist.id == FinalistCategory.finalist_id)
        .join(RubricCriterion, RubricCriterion.id == CalibrationItem.criterion_id)
        .where(CalibrationItem.study_id == study.id)
        .order_by(Finalist.full_name, RubricCriterion.position)
    ).all()
    mine = {
        r.item_id: r
        for r in session.scalars(
            select(CalibrationRating).where(
                CalibrationRating.rater_id == user.id,
                CalibrationRating.item_id.in_([item.id for item, *_ in rows]),
            )
        )
    }
    return {
        "study": _summary(session, study, user),
        "scale_levels": levels_out(session.get_one(RubricVersion, study.rubric_version_id).scale),
        "items": [
            {
                "id": str(item.id),
                "finalist_id": str(finalist_id),
                "finalist_name": name,
                "criterion": {
                    "name": c.name,
                    "description": c.description,
                    "evidence_expectations": c.evidence_expectations,
                },
                "rating": mine[item.id].rating if item.id in mine else None,
                "notes": mine[item.id].notes if item.id in mine else None,
            }
            for item, finalist_id, name, c in rows
        ],
    }


class RatingIn(BaseModel):
    item_id: uuid.UUID
    rating: int = Field(ge=0, le=5, description="Checked against the study's rubric scale")
    notes: str | None = Field(default=None, max_length=2000)


@router.put("/calibration-studies/{study_id}/ratings")
def save_rating(
    study_id: uuid.UUID, body: RatingIn, session: SessionDep, user: CurrentUser
) -> dict:
    study = _get_study(session, study_id)
    try:
        calibration.rate(
            session, study, user, body.item_id, body.rating, (body.notes or "").strip() or None
        )
    except CalibrationError as exc:
        raise _http(exc) from exc
    session.commit()
    return {"saved": True}


class SubmitIn(BaseModel):
    attest_blind: bool


@router.post("/calibration-studies/{study_id}/submit")
def submit(study_id: uuid.UUID, body: SubmitIn, session: SessionDep, user: CurrentUser) -> dict:
    study = _get_study(session, study_id)
    try:
        calibration.submit(session, study, user, body.attest_blind)
    except CalibrationError as exc:
        raise _http(exc) from exc
    session.commit()
    return _summary(session, study, user)


@router.get("/calibration-studies/{study_id}/results")
def results(study_id: uuid.UUID, session: SessionDep, user: StudyManager) -> dict:
    """Final results once closed; interim figures (submitted raters only) while open."""
    study = _get_study(session, study_id)
    if calibration.is_blinded(session, user, study.category_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "submit your own ratings before viewing calibration results"
        )
    data = study.results if study.status == "closed" else calibration.compute(session, study)
    return {
        "study": _summary(session, study, user),
        "final": study.status == "closed",
        "results": data,
    }


@router.post("/calibration-studies/{study_id}/close")
def close(study_id: uuid.UUID, session: SessionDep, user: StudyManager) -> dict:
    study = _get_study(session, study_id)
    try:
        data = calibration.close(session, study, user)
    except CalibrationError as exc:
        raise _http(exc) from exc
    session.commit()
    return {"study": _summary(session, study, user), "final": True, "results": data}
