"""Rights an administrator grants to each user group, stored in role_permissions.

Administrators hold every right. The rights that keep the system safe (managing users,
amending or unfreezing rubrics, retention, purging and category visibility) stay theirs alone
and aren't listed here.
"""

import enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, RolePermission, User


class Permission(enum.StrEnum):
    MANAGE_EVIDENCE = "manage_evidence"
    REVIEW_CLAIMS = "review_claims"
    APPROVE_RUBRICS = "approve_rubrics"
    GENERATE_REPORTS = "generate_reports"
    MANAGE_CALIBRATION = "manage_calibration"
    RATE_CALIBRATION = "rate_calibration"
    EXPORT_PERSONAL_DATA = "export_personal_data"
    SCORE_CANDIDATES = "score_candidates"
    VIEW_JUDGE_SCORES = "view_judge_scores"


# Shown on the administrator's Roles & rights page.
DESCRIPTIONS: dict[Permission, tuple[str, str]] = {
    Permission.MANAGE_EVIDENCE: (
        "Manage finalists and evidence",
        "Add categories and finalists, upload and withdraw documents, record consent and "
        "reference notes, draft rubrics, extract claims, and run assessments and rankings.",
    ),
    Permission.REVIEW_CLAIMS: (
        "Review evidence",
        "Verify or reject claims and external sources, and review or add criterion ratings.",
    ),
    Permission.APPROVE_RUBRICS: (
        "Approve rubrics",
        "Approve draft rubrics and freeze approved ones for production ranking.",
    ),
    Permission.GENERATE_REPORTS: (
        "Generate reports",
        "Generate category reports and judges' packs.",
    ),
    Permission.MANAGE_CALIBRATION: (
        "Run calibration studies",
        "Start and close calibration studies, choose raters and see the results.",
    ),
    Permission.RATE_CALIBRATION: (
        "Rate in calibration studies",
        "Can be chosen as a rater in a calibration study.",
    ),
    Permission.EXPORT_PERSONAL_DATA: (
        "Export a finalist's data",
        "Download everything held about a finalist (subject access request).",
    ),
    Permission.SCORE_CANDIDATES: (
        "Score candidates",
        "Give each finalist in the judges' pack a score from 0 to 3 with a comment. Each "
        "person sees only their own scores.",
    ),
    Permission.VIEW_JUDGE_SCORES: (
        "See the judges' tally",
        "See every judge's scores and comments, the averages and the winner, and close "
        "scoring. Never granted to judges, who score independently.",
    ),
}

# Rights a group can never hold (also enforced by the database).
NEVER: dict[Permission, tuple[Role, ...]] = {Permission.VIEW_JUDGE_SCORES: (Role.JUDGE,)}

# The groups whose rights an administrator can change.
GROUPS = (Role.SECRETARIAT, Role.REVIEWER, Role.JUDGE, Role.COMMITTEE)


def granted(session: Session, role: Role) -> frozenset[Permission]:
    if role == Role.ADMIN:
        return frozenset(Permission)
    return frozenset(
        Permission(p)
        for p in session.scalars(
            select(RolePermission.permission).where(RolePermission.role == role)
        )
    )


def has(session: Session, user: User, permission: Permission) -> bool:
    return permission in granted(session, user.role)


def roles_with(session: Session, permission: Permission) -> list[Role]:
    """Administrators and every group granted the right."""
    return [Role.ADMIN] + list(
        session.scalars(
            select(RolePermission.role).where(RolePermission.permission == permission.value)
        )
    )


def set_granted(session: Session, role: Role, permissions: set[Permission]) -> None:
    current = granted(session, role)
    for p in current - permissions:
        session.delete(session.get(RolePermission, (role, p.value)))
    for p in permissions - current:
        session.add(RolePermission(role=role, permission=p.value))
    session.flush()
