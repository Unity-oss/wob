"""Sign-in, sessions, invitations, two-factor authentication, user administration and the rights
of each user group."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep
from app.config import Settings, get_settings
from app.models import Role, User, UserSession
from app.security import accounts, permissions
from app.security.accounts import AuthError
from app.security.auth import SESSION_COOKIE, Admin, AuthenticatedUser, request_token
from app.security.permissions import Permission
from app.services import audit, email

router = APIRouter(prefix="/api/v1", tags=["authentication"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None, request.headers.get("user-agent"))


def _fail(
    session: Session, exc: AuthError, code: int = status.HTTP_401_UNAUTHORIZED
) -> HTTPException:
    session.commit()  # keep failed-attempt counters and audit rows
    return HTTPException(code, {"code": exc.code, "message": str(exc)})


def me_payload(session: Session, user: User, settings: Settings) -> dict:
    granted = permissions.granted(session, user.role)
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "mfa_enabled": user.mfa_enabled,
        "mfa_setup_required": accounts.mfa_required(user, settings) and not user.mfa_enabled,
        "permissions": {
            **{p.value: p in granted for p in Permission},
            "administer_rubrics": user.role == Role.ADMIN,  # amend approved, unfreeze frozen
            "manage_users": user.role == Role.ADMIN,
        },
    }


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


# --- Sign-in -----------------------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    otp: str | None = Field(default=None, max_length=12)
    # API clients ask for a bearer token in the body; browsers get an HttpOnly cookie only.
    issue_bearer_token: bool = False


@router.post("/auth/login")
def login(
    body: LoginIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict:
    ip, agent = _client(request)
    try:
        user = accounts.authenticate(session, body.email, body.password, body.otp, settings, ip)
    except AuthError as exc:
        raise _fail(session, exc) from exc
    kind = "api" if body.issue_bearer_token else "browser"
    token, expires = accounts.create_session(
        session, user, settings, kind=kind, ip=ip, user_agent=agent
    )
    session.commit()
    result = {"user": me_payload(session, user, settings), "expires_at": expires.isoformat()}
    if body.issue_bearer_token:
        result["token"] = token
    else:
        _set_cookie(response, token, settings)
    return result


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: SessionDep) -> None:
    token = request_token(request)
    if token:
        accounts.revoke_token(session, token)
        session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me")
def me(user: AuthenticatedUser, session: SessionDep, settings: SettingsDep) -> dict:
    """The signed-in user and what they may do, so the UI shows the right controls."""
    return me_payload(session, user, settings)


class AcceptInviteIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/accept-invite")
def accept_invite(
    body: AcceptInviteIn,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict:
    try:
        user = accounts.accept_invite(session, body.token, body.password, settings)
    except AuthError as exc:
        raise _fail(session, exc, status.HTTP_400_BAD_REQUEST) from exc
    ip, agent = _client(request)
    token, expires = accounts.create_session(session, user, settings, ip=ip, user_agent=agent)
    session.commit()
    _set_cookie(response, token, settings)
    return {"user": me_payload(session, user, settings), "expires_at": expires.isoformat()}


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChangeIn,
    request: Request,
    user: AuthenticatedUser,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    if not accounts.check_password(user, body.current_password):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "invalid_credentials", "message": "current password is wrong"},
        )
    try:
        accounts.set_password(user, body.new_password, settings)
    except AuthError as exc:
        raise _fail(session, exc, status.HTTP_400_BAD_REQUEST) from exc
    # Sign out everywhere else.
    current = accounts.token_hash(request_token(request) or "")
    session.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.token_hash != current,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=accounts.now())
    )
    audit.record(session, user, "auth.password_changed", "user", user.id)
    session.commit()


# --- Two-factor authentication -----------------------------------------------------------------


@router.post("/auth/mfa/setup")
def mfa_setup(user: AuthenticatedUser, session: SessionDep, settings: SettingsDep) -> dict:
    """A new secret for an authenticator app. It becomes active once a code is confirmed."""
    try:
        secret, uri = accounts.start_mfa_setup(user, settings)
    except AuthError as exc:
        raise _fail(session, exc, status.HTTP_409_CONFLICT) from exc
    session.commit()
    return {"secret": secret, "otpauth_uri": uri}


class OtpIn(BaseModel):
    otp: str = Field(min_length=6, max_length=12)


@router.post("/auth/mfa/enable")
def mfa_enable(
    body: OtpIn, user: AuthenticatedUser, session: SessionDep, settings: SettingsDep
) -> dict:
    try:
        accounts.enable_mfa(session, user, body.otp, settings)
    except AuthError as exc:
        raise _fail(session, exc, status.HTTP_400_BAD_REQUEST) from exc
    session.commit()
    return me_payload(session, user, settings)


# --- User administration (admins) --------------------------------------------------------------


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_active: bool
    mfa_enabled: bool
    has_password: bool
    last_login_at: str | None
    locked: bool


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
        has_password=user.password_hash is not None,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        locked=bool(user.locked_until and user.locked_until > accounts.now()),
    )


@router.get("/users")
def list_users(session: SessionDep, _: Admin) -> list[UserOut]:
    return [_user_out(u) for u in session.scalars(select(User).order_by(User.display_name))]


class UserCreateIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role: Role


def _invitation_out(
    session: Session,
    settings: Settings,
    user: User,
    link: str,
    admin: User,
    *,
    reset: bool,
) -> dict:
    """Email the link (after commit, so it is valid) and report what happened. The link is also
    returned so the admin can share it another way if the email doesn't arrive."""
    sent, error = False, None
    if email.enabled(settings):
        try:
            email.send_invitation(settings, user, link, admin, reset=reset)
            sent = True
        except email.EmailError as exc:
            error = str(exc)
        audit.record(
            session,
            admin,
            "auth.invite_emailed" if sent else "auth.invite_email_failed",
            "user",
            user.id,
            **({} if sent else {"error": error}),
        )
        session.commit()
    return {
        "user": _user_out(user),
        "invite_link": link,
        "expires_in_hours": settings.invite_ttl_hours,
        "email_sent": sent,
        "email_error": error,
        "email_enabled": email.enabled(settings),
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateIn, session: SessionDep, settings: SettingsDep, admin: Admin
) -> dict:
    """Create an account, email its one-time invitation link, and return the link too."""
    if session.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "a user with this email exists")
    user = User(id=uuid.uuid4(), email=body.email, display_name=body.display_name, role=body.role)
    session.add(user)
    session.flush()
    audit.record(session, admin, "user.created", "user", user.id, role=body.role.value)
    link = accounts.create_invite(session, user, admin, settings)
    session.commit()
    return _invitation_out(session, settings, user, link, admin, reset=False)


class UserUpdateIn(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


@router.patch("/users/{user_id}")
def update_user(
    user_id: uuid.UUID, body: UserUpdateIn, session: SessionDep, admin: Admin
) -> UserOut:
    user = session.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if user.id == admin.id and (body.is_active is False or (body.role and body.role != Role.ADMIN)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "you can't remove your own admin access; ask another admin"
        )
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(user, field, value)
    if "role" in changes or changes.get("is_active") is False:
        accounts.revoke_all_sessions(session, user.id)  # take effect immediately
    audit.record(
        session,
        admin,
        "user.updated",
        "user",
        user.id,
        changes={k: str(v) for k, v in changes.items()},
    )
    session.commit()
    return _user_out(user)


@router.post("/users/{user_id}/invite")
def reissue_invite(
    user_id: uuid.UUID, session: SessionDep, settings: SettingsDep, admin: Admin
) -> dict:
    """A new set-password link (for a lost invite or a forgotten password). Signs the user out,
    and resets two-factor authentication so they can set it up again."""
    user = session.get(User, user_id, with_for_update=True)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active user not found")
    user.password_hash = None
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.locked_until = None
    user.failed_logins = 0
    accounts.revoke_all_sessions(session, user.id)
    link = accounts.create_invite(session, user, admin, settings)
    session.commit()
    return _invitation_out(session, settings, user, link, admin, reset=True)


# --- Rights of each user group (admins) --------------------------------------------------------


@router.get("/role-permissions")
def list_role_permissions(session: SessionDep, _: Admin) -> dict:
    """The rights an administrator can grant, and which user groups hold each."""
    return {
        "permissions": [
            {
                "key": p.value,
                "label": label,
                "description": description,
                "never_for": [r.value for r in permissions.NEVER.get(p, ())],
            }
            for p, (label, description) in permissions.DESCRIPTIONS.items()
        ],
        "groups": [
            {"role": role.value, "permissions": sorted(permissions.granted(session, role))}
            for role in permissions.GROUPS
        ],
    }


class RolePermissionsIn(BaseModel):
    permissions: list[Permission] = Field(max_length=len(Permission))


@router.put("/role-permissions/{role}")
def update_role_permissions(
    role: Role, body: RolePermissionsIn, session: SessionDep, admin: Admin
) -> dict:
    """Set a user group's rights. They apply to its members from their next request."""
    if role not in permissions.GROUPS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "administrators always hold every right"
        )
    barred = [p for p in body.permissions if role in permissions.NEVER.get(p, ())]
    if barred:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{email.ROLE_NAMES[role]}s can't be given: "
            + ", ".join(permissions.DESCRIPTIONS[p][0] for p in barred),
        )
    before = sorted(permissions.granted(session, role))
    after = sorted(set(body.permissions))
    permissions.set_granted(session, role, set(after))
    if before != after:
        audit.record(
            session,
            admin,
            "role.permissions_updated",
            "role",
            None,
            role=role.value,
            before=before,
            after=after,
        )
    session.commit()
    return {"role": role.value, "permissions": after}
