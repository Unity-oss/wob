"""Versioned rubrics (spec §7): drafts are editable; approved and frozen ones are not, except
that an administrator may amend an approved rubric and unfreeze a frozen one (the database
enforces this too). Production rankings need a frozen rubric."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from app.models import (
    AssessmentRun,
    CalibrationItem,
    CalibrationStudy,
    CriterionAssessment,
    RankingSnapshot,
    Role,
    RubricCriterion,
    RubricStatus,
    RubricVersion,
    User,
    WeightingMode,
)
from app.services import audit
from app.services.scales import RatingScale, Scale, scale_of

TEMPLATE_NOTE = (
    "DRAFT TEMPLATE: illustrative criteria to exercise the system. They are not committee "
    "policy and must be replaced by the committee's approved criteria before any real ranking."
)

DRAFT_TEMPLATE: list[dict[str, Any]] = [
    {
        "code": "governance-leadership",
        "name": "Board leadership and governance",
        "description": "Leadership on boards or board committees: chairing, strategic "
        "oversight and governance improvements the nominee led.",
        "evidence_expectations": "Board and committee roles with dates; specific governance "
        "decisions or reforms the nominee led; board evaluation results.",
    },
    {
        "code": "measurable-impact",
        "name": "Measurable organisational impact",
        "description": "Results for the organisations the nominee serves that can be "
        "attributed to the nominee's board work.",
        "evidence_expectations": "Quantified outcomes with dates and sources, such as annual "
        "reports or audited figures; how the nominee contributed.",
    },
    {
        "code": "advancing-women",
        "name": "Advancing women in leadership",
        "description": "Mentoring, sponsorship and advocacy that increase women's "
        "representation and influence on boards.",
        "evidence_expectations": "Named programmes, numbers mentored or sponsored, resulting "
        "appointments, policies championed.",
    },
    {
        "code": "integrity-stewardship",
        "name": "Ethics, integrity and stewardship",
        "description": "Stewardship of risk, compliance, accountability and sustainability.",
        "evidence_expectations": "Audit, risk or ethics roles; policies introduced; "
        "regulatory or stakeholder outcomes.",
    },
    {
        "code": "recognition-influence",
        "name": "Recognition and sector influence",
        "description": "Recognition from credible bodies and influence on governance practice "
        "beyond the nominee's own boards. Not popularity or online following.",
        "evidence_expectations": "Awards with issuer and year; invited roles on standard-"
        "setting or professional bodies; published governance work.",
    },
]


class RubricError(ValueError):
    def __init__(self, message: str, *, conflict: bool = False, forbidden: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict
        self.forbidden = forbidden


def _criteria(rubric: RubricVersion, items: list[dict[str, Any]]) -> list[RubricCriterion]:
    codes = [i["code"] for i in items]
    if not items:
        raise RubricError("a rubric needs at least one criterion")
    if len(set(codes)) != len(codes):
        raise RubricError("criterion codes must be unique")
    return [
        RubricCriterion(
            id=uuid.uuid4(),
            rubric_version_id=rubric.id,
            code=i["code"],
            name=i["name"],
            description=i["description"],
            evidence_expectations=i.get("evidence_expectations"),
            weight=Decimal(str(i["weight"])) if i.get("weight") is not None else None,
            position=n,
        )
        for n, i in enumerate(items, start=1)
    ]


def create_draft(
    session: Session,
    category_id: uuid.UUID,
    user: User,
    criteria: list[dict[str, Any]],
    *,
    weighting: WeightingMode = WeightingMode.EQUAL,
    notes: str | None = None,
    scale: RatingScale = RatingScale.ZERO_TO_THREE,
    copied_from: RubricVersion | None = None,
) -> RubricVersion:
    version = (
        session.scalar(
            select(func.max(RubricVersion.version)).where(RubricVersion.category_id == category_id)
        )
        or 0
    ) + 1
    rubric = RubricVersion(
        id=uuid.uuid4(),
        category_id=category_id,
        version=version,
        weighting=weighting,
        notes=notes,
        scale=scale.value,
    )
    session.add(rubric)
    session.flush()
    session.add_all(_criteria(rubric, criteria))
    audit.record(
        session,
        user,
        "rubric.created",
        "rubric_version",
        rubric.id,
        version=version,
        criteria=[c["code"] for c in criteria],
        copied_from_version=copied_from.version if copied_from else None,
    )
    return rubric


def copy_of(rubric: RubricVersion) -> tuple[list[dict[str, Any]], WeightingMode, str | None]:
    """A rubric's criteria, weighting and notes, to start a new draft version from. The
    template's warning note isn't carried over."""
    notes = None if rubric.notes == TEMPLATE_NOTE else rubric.notes
    return [_criterion_dump(c) for c in rubric.criteria], rubric.weighting, notes


def _admin_change(session: Session) -> None:
    """Let this transaction past the database guards on non-draft rubrics (migration 0013)."""
    session.execute(text("SET LOCAL wob.rubric_admin = 'on'"))


def _criterion_dump(c: RubricCriterion) -> dict[str, Any]:
    return {
        "code": c.code,
        "name": c.name,
        "description": c.description,
        "evidence_expectations": c.evidence_expectations,
        "weight": str(c.weight) if c.weight is not None else None,
    }


def _rated(session: Session, criterion_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Criteria that assessments or calibration studies already refer to."""
    if not criterion_ids:
        return set()
    return set(
        session.scalars(
            select(CriterionAssessment.criterion_id).where(
                CriterionAssessment.criterion_id.in_(criterion_ids)
            )
        )
    ) | set(
        session.scalars(
            select(CalibrationItem.criterion_id).where(
                CalibrationItem.criterion_id.in_(criterion_ids)
            )
        )
    )


def _sync_criteria(session: Session, rubric: RubricVersion, items: list[dict[str, Any]]) -> None:
    """Update criteria in place by code, so ratings already made keep pointing at them. A
    criterion that has ratings can be reworded but not removed."""
    wanted = _criteria(rubric, items)  # validates codes
    existing = {c.code: c for c in rubric.criteria}
    keep = {c.code for c in wanted}
    removed = [c for code, c in existing.items() if code not in keep]
    blocked = _rated(session, [c.id for c in removed])
    if blocked:
        names = ", ".join(sorted(c.name for c in removed if c.id in blocked))
        raise RubricError(
            f"criteria already rated can't be removed ({names}); reword them, or create a new "
            "rubric version",
            conflict=True,
        )
    for c in removed:
        session.delete(c)
    # Move kept criteria out of the way first: positions are unique within a rubric.
    for c in existing.values():
        if c.code in keep:
            c.position += 1000
    session.flush()
    for new in wanted:
        old = existing.get(new.code)
        if old is None:
            session.add(new)
            continue
        old.name = new.name
        old.description = new.description
        old.evidence_expectations = new.evidence_expectations
        old.weight = new.weight
        old.position = new.position
    session.flush()
    session.expire(rubric, ["criteria"])


def update_rubric(
    session: Session,
    rubric: RubricVersion,
    user: User,
    criteria: list[dict[str, Any]],
    *,
    weighting: WeightingMode,
    notes: str | None,
    scale: RatingScale | None = None,
) -> None:
    """Edit a draft; administrators may also amend an approved rubric. An amendment stays
    approved under the administrator's name, with the before and after recorded in the audit
    log. Frozen rubrics must be unfrozen first."""
    if rubric.status == RubricStatus.FROZEN:
        raise RubricError(
            f"rubric version {rubric.version} is frozen; an administrator must unfreeze it "
            "before it can be changed",
            conflict=True,
        )
    amending = rubric.status == RubricStatus.APPROVED
    if amending and user.role != Role.ADMIN:
        raise RubricError(
            f"rubric version {rubric.version} is approved; only an administrator can amend it",
            forbidden=True,
        )
    if scale is not None and scale.value != rubric.scale:
        if amending or _rated(session, [c.id for c in rubric.criteria]):
            raise RubricError(
                "the rating scale can only change on a draft nobody has rated yet; create a "
                "new rubric version",
                conflict=True,
            )
        rubric.scale = scale.value
    if amending:
        _admin_change(session)
    before = [_criterion_dump(c) for c in rubric.criteria]
    before_weighting = rubric.weighting.value
    _sync_criteria(session, rubric, criteria)
    rubric.weighting = weighting
    rubric.notes = notes
    if amending:
        check_weights(rubric)
        rubric.approved_by = user.id
        rubric.approved_at = datetime.now(UTC)
        audit.record(
            session,
            user,
            "rubric.amended",
            "rubric_version",
            rubric.id,
            before={"weighting": before_weighting, "criteria": before},
            after={
                "weighting": weighting.value,
                "criteria": [_criterion_dump(c) for c in rubric.criteria],
            },
        )
    else:
        audit.record(
            session,
            user,
            "rubric.updated",
            "rubric_version",
            rubric.id,
            criteria=[c["code"] for c in criteria],
            weighting=weighting.value,
        )
    session.flush()


def check_weights(rubric: RubricVersion) -> None:
    if rubric.weighting == WeightingMode.WEIGHTED and any(
        c.weight is None for c in rubric.criteria
    ):
        raise RubricError("every criterion needs a weight for weighted scoring")


def approve(session: Session, rubric: RubricVersion, user: User) -> None:
    if rubric.status != RubricStatus.DRAFT:
        raise RubricError(f"rubric is already {rubric.status.value}", conflict=True)
    check_weights(rubric)
    rubric.status = RubricStatus.APPROVED
    rubric.approved_by = user.id
    rubric.approved_at = datetime.now(UTC)
    audit.record(session, user, "rubric.approved", "rubric_version", rubric.id)


def freeze(session: Session, rubric: RubricVersion, user: User) -> None:
    if rubric.status != RubricStatus.APPROVED:
        raise RubricError("only an approved rubric can be frozen", conflict=True)
    rubric.status = RubricStatus.FROZEN
    rubric.frozen_at = datetime.now(UTC)
    audit.record(session, user, "rubric.frozen", "rubric_version", rubric.id)


def unfreeze(session: Session, rubric: RubricVersion, user: User, reason: str) -> None:
    """Administrators only: return a frozen rubric to approved so it can be amended. Rankings
    already calculated keep the status they were taken under."""
    if user.role != Role.ADMIN:
        raise RubricError("only an administrator can unfreeze a rubric", forbidden=True)
    if rubric.status != RubricStatus.FROZEN:
        raise RubricError(f"rubric is {rubric.status.value}, not frozen", conflict=True)
    reason = reason.strip()
    if not reason:
        raise RubricError("give a reason for unfreezing the rubric")
    _admin_change(session)
    rubric.status = RubricStatus.APPROVED
    rubric.frozen_at = None
    audit.record(session, user, "rubric.unfrozen", "rubric_version", rubric.id, reason=reason)


def current_rubric(session: Session, category_id: uuid.UUID) -> RubricVersion | None:
    """The rubric assessments use: the latest frozen version, else the latest of any status."""
    rubrics = session.scalars(
        select(RubricVersion)
        .where(RubricVersion.category_id == category_id)
        .order_by(RubricVersion.version.desc())
    ).all()
    frozen = [r for r in rubrics if r.status == RubricStatus.FROZEN]
    return frozen[0] if frozen else (rubrics[0] if rubrics else None)


def run_scale(session: Session, run_id: uuid.UUID) -> Scale:
    """The rating scale of an assessment run: its rubric version's."""
    return scale_of(
        session.scalar(
            select(RubricVersion.scale)
            .join(AssessmentRun, AssessmentRun.rubric_version_id == RubricVersion.id)
            .where(AssessmentRun.id == run_id)
        )
    )


def working_rubric(session: Session, category_id: uuid.UUID) -> RubricVersion | None:
    """The newest version, when it isn't the one in use: a draft or approved version being
    prepared while an earlier frozen version stays in use until it is frozen."""
    latest = session.scalar(
        select(RubricVersion)
        .where(RubricVersion.category_id == category_id)
        .order_by(RubricVersion.version.desc())
        .limit(1)
    )
    current = current_rubric(session, category_id)
    return latest if latest and current and latest.id != current.id else None


def discard_draft(session: Session, rubric: RubricVersion, user: User) -> None:
    """Delete a draft nobody has used, e.g. one started from the template by mistake. A draft an
    assessment run, ranking or calibration study used stays, as their record."""
    if rubric.status != RubricStatus.DRAFT:
        raise RubricError(
            f"only drafts can be discarded; this one is {rubric.status.value}", conflict=True
        )
    used = session.scalar(
        select(
            exists().where(AssessmentRun.rubric_version_id == rubric.id)
            | exists().where(RankingSnapshot.rubric_version_id == rubric.id)
            | exists().where(CalibrationStudy.rubric_version_id == rubric.id)
        )
    )
    if used:
        raise RubricError(
            "this draft was used for an assessment run, ranking or calibration study, so it is "
            "kept; edit it instead",
            conflict=True,
        )
    audit.record(
        session,
        user,
        "rubric.discarded",
        "rubric_version",
        rubric.id,
        version=rubric.version,
        criteria=[c.code for c in rubric.criteria],
    )
    for criterion in list(rubric.criteria):
        session.delete(criterion)
    session.flush()
    session.delete(rubric)
