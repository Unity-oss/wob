"""Judges' scores: each judge scores finalists 0-3 with a comment, seeing only their own, and
the tally (hidden from judges) averages them to choose each category's winner."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep, _get_category, _get_finalist_category
from app.models import AwardCategory, User
from app.security.auth import Admin, JudgeUser, ScoreViewer
from app.services import consent, judging

router = APIRouter(prefix="/api/v1", tags=["judging"])


class JudgeScoreIn(BaseModel):
    score: int = Field(ge=0, le=judging.MAX_SCORE)
    comment: str = Field(max_length=4000)


class MyScoreOut(BaseModel):
    score: int
    comment: str
    updated_at: datetime


@router.put("/categories/{category_id}/finalists/{finalist_id}/judge-score")
def save_judge_score(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    body: JudgeScoreIn,
    session: SessionDep,
    judge: JudgeUser,
) -> MyScoreOut:
    """Record or revise your own score for a finalist. Other judges' scores are never shown."""
    category = session.get(AwardCategory, category_id, with_for_update=True)
    if category is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    fc = _get_finalist_category(session, category_id, finalist_id)
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "add a comment with your score")
    if consent.withdrawn(session, fc.id, consent.Purpose.ASSESSMENT):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the finalist has withdrawn consent to assessment (part A)"
        )
    try:
        row = judging.save_score(session, category, fc, judge, body.score, comment)
    except judging.JudgingClosed as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return MyScoreOut(score=row.score, comment=row.comment, updated_at=row.updated_at)


class TallyScore(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    score: int
    comment: str
    updated_at: datetime


class TallyJudge(BaseModel):
    id: uuid.UUID
    name: str


class TallyCandidate(BaseModel):
    finalist_id: uuid.UUID
    full_name: str
    organisation: str | None
    rank: int
    is_tie: bool
    judges_scored: int
    total: int
    average: str
    scores: list[TallyScore]


class TallyCategory(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    expected_judges: int
    max_score: int
    judges: list[TallyJudge]
    complete: bool
    # winner: one leader and every expected score is in; provisional: one leader so far;
    # tie: leaders share the top average; no_scores: nobody has scored yet.
    status: str
    leaders: list[uuid.UUID]
    closed_at: datetime | None
    closed_by: str | None
    candidates: list[TallyCandidate]


def _tally_out(session: Session, category: AwardCategory) -> TallyCategory:
    result = judging.tally(session, category)
    leaders = result.leaders
    if not leaders:
        state = "no_scores"
    elif len(leaders) > 1:
        state = "tie"
    else:
        state = "winner" if result.complete else "provisional"
    closed_by = (
        session.get(User, category.judging_closed_by) if category.judging_closed_by else None
    )
    return TallyCategory(
        id=category.id,
        code=category.code,
        name=category.name,
        expected_judges=judging.EXPECTED_JUDGES,
        max_score=judging.MAX_SCORE,
        judges=[TallyJudge(id=j, name=name) for j, name in result.judges],
        complete=result.complete,
        status=state,
        leaders=[c.finalist_id for c in leaders],
        closed_at=category.judging_closed_at,
        closed_by=closed_by.display_name if closed_by else None,
        candidates=[
            TallyCandidate(
                finalist_id=c.finalist_id,
                full_name=c.full_name,
                organisation=c.organisation,
                rank=c.rank,
                is_tie=c.is_tie,
                judges_scored=len(c.scores),
                total=c.total,
                average=judging.display_average(c.average),
                scores=[
                    TallyScore(judge_id=j, judge_name=n, score=s, comment=text_, updated_at=at)
                    for j, n, s, text_, at in c.scores
                ],
            )
            for c in result.candidates
        ],
    )


@router.get("/judge-scores")
def judge_score_tally(session: SessionDep, _: ScoreViewer) -> list[TallyCategory]:
    """Every category's judges' scores, totals and averages, ranked by average. Judges can't
    see this."""
    categories = session.scalars(
        select(AwardCategory).where(AwardCategory.hidden_at.is_(None)).order_by(AwardCategory.code)
    )
    return [_tally_out(session, c) for c in categories]


@router.post("/categories/{category_id}/judge-scores/close")
def close_judging(category_id: uuid.UUID, session: SessionDep, user: ScoreViewer) -> TallyCategory:
    """Stop judges changing their scores, e.g. once the winner is confirmed."""
    category = _get_category(session, category_id)
    judging.close(session, category, user)
    session.commit()
    return _tally_out(session, category)


class ReopenIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/categories/{category_id}/judge-scores/reopen")
def reopen_judging(
    category_id: uuid.UUID, body: ReopenIn, session: SessionDep, admin: Admin
) -> TallyCategory:
    """Administrators only: let judges change their scores again."""
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give a reason for reopening")
    category = _get_category(session, category_id)
    judging.reopen(session, category, admin, reason)
    session.commit()
    return _tally_out(session, category)
