"""Data protection controls: consent records, retention, purge and subject-access export
(docs/operations/data-protection-review.md)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep, _get_category, _get_finalist_category
from app.models import FinalistConsent, User
from app.security.auth import Admin, Coordinator, CurrentUser, Exporter
from app.services import audit, consent
from app.services.http import attachment
from app.services.purge import purge_finalist
from app.services.storage import LocalStorage, get_storage
from app.services.subject_export import build_export

router = APIRouter(prefix="/api/v1", tags=["data protection"])
StorageDep = Annotated[LocalStorage, Depends(get_storage)]


class ConsentIn(BaseModel):
    notice_version: str = Field(min_length=1, max_length=50)
    assessment: bool
    ai_processing: bool
    web_research: bool
    method: Literal["signed_form", "email", "online", "other"]
    reference: str | None = Field(default=None, max_length=500)


class ConsentOut(BaseModel):
    notice_version: str
    assessment: bool
    ai_processing: bool
    web_research: bool
    method: str
    reference: str | None
    recorded_by: str | None
    recorded_at: datetime


def _consent_out(session: Session, record: FinalistConsent) -> ConsentOut:
    user = session.get(User, record.recorded_by) if record.recorded_by else None
    return ConsentOut(
        notice_version=record.notice_version,
        assessment=record.assessment,
        ai_processing=record.ai_processing,
        web_research=record.web_research,
        method=record.method,
        reference=record.reference,
        recorded_by=user.display_name if user else None,
        recorded_at=record.recorded_at,
    )


@router.get("/categories/{category_id}/finalists/{finalist_id}/consent")
def get_consent(
    category_id: uuid.UUID, finalist_id: uuid.UUID, session: SessionDep, _: CurrentUser
) -> dict:
    fc = _get_finalist_category(session, category_id, finalist_id)
    history = session.scalars(
        select(FinalistConsent)
        .where(FinalistConsent.finalist_category_id == fc.id)
        .order_by(FinalistConsent.recorded_at.desc(), FinalistConsent.id.desc())
    ).all()
    return {
        "current": _consent_out(session, history[0]) if history else None,
        "history": [_consent_out(session, c) for c in history],
    }


@router.post(
    "/categories/{category_id}/finalists/{finalist_id}/consent", status_code=status.HTTP_201_CREATED
)
def record_consent(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    body: ConsentIn,
    session: SessionDep,
    user: Coordinator,
) -> ConsentOut:
    """Record the finalist's consent as given (or withdrawn) on the signed notice."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    try:
        record = consent.record(session, fc.id, user, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    session.refresh(record)
    return _consent_out(session, record)


class RetentionIn(BaseModel):
    retention_until: date | None


@router.put("/categories/{category_id}/retention")
def set_retention(
    category_id: uuid.UUID, body: RetentionIn, session: SessionDep, admin: Admin
) -> dict:
    """The date after which the category's personal data is destroyed (s.18)."""
    category = _get_category(session, category_id)
    category.retention_until = body.retention_until
    audit.record(
        session,
        admin,
        "retention.set",
        "award_category",
        category.id,
        retention_until=body.retention_until.isoformat() if body.retention_until else None,
    )
    session.commit()
    return {"retention_until": body.retention_until}


@router.get("/categories/{category_id}/retention")
def get_retention(category_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> dict:
    return {"retention_until": _get_category(session, category_id).retention_until}


class PurgeIn(BaseModel):
    reason: str = Field(
        min_length=5,
        max_length=500,
        description="Why, without personal details, e.g. 'erasure request #12'",
    )
    confirm_name: str


@router.post("/categories/{category_id}/finalists/{finalist_id}/purge")
def purge(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    body: PurgeIn,
    session: SessionDep,
    storage: StorageDep,
    admin: Admin,
) -> dict:
    """Permanently delete everything held about this nomination (s.16, s.18(4))."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    if body.confirm_name.strip().casefold() != fc.finalist.full_name.strip().casefold():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "type the finalist's full name exactly to confirm",
        )
    counts = purge_finalist(session, storage, fc.id, admin, body.reason.strip())
    return {"purged": True, "counts": counts}


@router.get("/categories/{category_id}/finalists/{finalist_id}/export")
def export(
    category_id: uuid.UUID,
    finalist_id: uuid.UUID,
    session: SessionDep,
    storage: StorageDep,
    user: Exporter,
) -> Response:
    """A ZIP of everything held about the nomination, for an access request (s.24)."""
    fc = _get_finalist_category(session, category_id, finalist_id)
    data, filename = build_export(session, storage, fc.id)
    audit.record(
        session, user, "subject_access.exported", "finalist_category", fc.id, size_bytes=len(data)
    )
    session.commit()
    return attachment(data, filename, "application/zip")
