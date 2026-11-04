"""Request authentication: a session cookie (browser) or Authorization: Bearer (API clients).

Users in MFA_REQUIRED_ROLES must turn on two-factor authentication before they can use
anything except their own account endpoints. What each user group may do is set by an
administrator (app/security/permissions.py).
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Role, User
from app.security.accounts import mfa_required, resolve_session
from app.security.permissions import Permission, has

SESSION_COOKIE = "wob_session"


def request_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def get_authenticated_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Any signed-in user, even one who still has to set up two-factor authentication."""
    token = request_token(request)
    user = resolve_session(session, token, settings) if token else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "sign in required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_user(
    user: Annotated[User, Depends(get_authenticated_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if mfa_required(user, settings) and not user.mfa_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "mfa_setup_required: your role requires two-factor authentication; set it up "
            "under Account",
        )
    return user


def require_roles(*roles: Role) -> Callable[[User], User]:
    def dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return dependency


def require_permission(permission: Permission) -> Callable[[User, Session], User]:
    def dependency(
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_session)],
    ) -> User:
        if not has(session, user, permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "your user group lacks this right")
        return user

    return dependency


AuthenticatedUser = Annotated[User, Depends(get_authenticated_user)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Coordinator = Annotated[User, Depends(require_permission(Permission.MANAGE_EVIDENCE))]
Reviewer = Annotated[User, Depends(require_permission(Permission.REVIEW_CLAIMS))]
RubricApprover = Annotated[User, Depends(require_permission(Permission.APPROVE_RUBRICS))]
ReportAuthor = Annotated[User, Depends(require_permission(Permission.GENERATE_REPORTS))]
StudyManager = Annotated[User, Depends(require_permission(Permission.MANAGE_CALIBRATION))]
Exporter = Annotated[User, Depends(require_permission(Permission.EXPORT_PERSONAL_DATA))]
JudgeUser = Annotated[User, Depends(require_permission(Permission.SCORE_CANDIDATES))]
ScoreViewer = Annotated[User, Depends(require_permission(Permission.VIEW_JUDGE_SCORES))]
Admin = Annotated[User, Depends(require_roles(Role.ADMIN))]
