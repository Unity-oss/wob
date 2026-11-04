"""Judges' independent scores (0-3 with a comment) and the tally that chooses each category's
winner: the finalist with the highest average score."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AwardCategory, Finalist, FinalistCategory, JudgeScore, User
from app.services import audit

EXPECTED_JUDGES = 3
MAX_SCORE = 3


class JudgingClosed(Exception):
    pass


def save_score(
    session: Session,
    category: AwardCategory,
    fc: FinalistCategory,
    judge: User,
    score: int,
    comment: str,
) -> JudgeScore:
    """Record or revise the judge's own score while scoring is open."""
    if category.judging_closed_at is not None:
        raise JudgingClosed("scoring for this category is closed")
    row = session.scalar(
        select(JudgeScore).where(
            JudgeScore.finalist_category_id == fc.id, JudgeScore.judge_id == judge.id
        )
    )
    if row is None:
        row = JudgeScore(
            finalist_category_id=fc.id, judge_id=judge.id, score=score, comment=comment
        )
        session.add(row)
    else:
        row.score, row.comment, row.updated_at = score, comment, datetime.now(UTC)
    # The comment stays out of the audit log; it is personal data about the finalist.
    audit.record(session, judge, "judge_score.saved", "finalist_category", fc.id, score=score)
    session.flush()
    return row


def own_scores(
    session: Session, judge: User, fc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, JudgeScore]:
    return {
        s.finalist_category_id: s
        for s in session.scalars(
            select(JudgeScore).where(
                JudgeScore.judge_id == judge.id, JudgeScore.finalist_category_id.in_(fc_ids)
            )
        )
    }


@dataclass
class CandidateTally:
    finalist_id: uuid.UUID
    full_name: str
    organisation: str | None
    # (judge id, judge name, score, comment, updated_at)
    scores: list[tuple[uuid.UUID, str, int, str, datetime]] = field(default_factory=list)
    rank: int = 0
    is_tie: bool = False

    @property
    def total(self) -> int:
        return sum(s[2] for s in self.scores)

    @property
    def average(self) -> Fraction:
        return Fraction(self.total, len(self.scores))


@dataclass
class CategoryTally:
    category: AwardCategory
    judges: list[tuple[uuid.UUID, str]]
    candidates: list[CandidateTally]

    @property
    def complete(self) -> bool:
        """At least three judges, each of whom scored every candidate."""
        return len(self.judges) >= EXPECTED_JUDGES and all(
            len(c.scores) == len(self.judges) for c in self.candidates
        )

    @property
    def leaders(self) -> list[CandidateTally]:
        return [c for c in self.candidates if c.rank == 1]


def tally(session: Session, category: AwardCategory) -> CategoryTally:
    rows = session.execute(
        select(JudgeScore, User.display_name, Finalist)
        .join(User, User.id == JudgeScore.judge_id)
        .join(FinalistCategory, FinalistCategory.id == JudgeScore.finalist_category_id)
        .join(Finalist, Finalist.id == FinalistCategory.finalist_id)
        .where(FinalistCategory.category_id == category.id)
        .order_by(User.display_name, JudgeScore.created_at)
    ).all()
    judges: dict[uuid.UUID, str] = {}
    candidates: dict[uuid.UUID, CandidateTally] = {}
    for score, judge_name, finalist in rows:
        judges.setdefault(score.judge_id, judge_name)
        candidate = candidates.setdefault(
            finalist.id, CandidateTally(finalist.id, finalist.full_name, finalist.organisation)
        )
        candidate.scores.append(
            (score.judge_id, judge_name, score.score, score.comment, score.updated_at)
        )
    ordered = sorted(candidates.values(), key=lambda c: (-c.average, c.full_name))
    # Competition ranking on the exact average: equal averages share a rank.
    for c in ordered:
        c.rank = 1 + sum(o.average > c.average for o in ordered)
        c.is_tie = sum(o.average == c.average for o in ordered) > 1
    return CategoryTally(category, sorted(judges.items(), key=lambda j: j[1]), ordered)


def display_average(value: Fraction) -> str:
    return str(
        (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def close(session: Session, category: AwardCategory, user: User) -> None:
    if category.judging_closed_at is None:
        category.judging_closed_at = datetime.now(UTC)
        category.judging_closed_by = user.id
        audit.record(session, user, "judging.closed", "award_category", category.id)


def reopen(session: Session, category: AwardCategory, user: User, reason: str) -> None:
    if category.judging_closed_at is not None:
        category.judging_closed_at = None
        category.judging_closed_by = None
        audit.record(
            session, user, "judging.reopened", "award_category", category.id, reason=reason
        )
