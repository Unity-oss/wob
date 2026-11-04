"""Deliberation reports (FR-11; spec §9 POST /reports, GET /reports/{id})."""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep, _get_category, _get_finalist_category
from app.models import (
    Finalist,
    FinalistCategory,
    JobStatus,
    RankingSnapshot,
    Report,
    ReportKind,
)
from app.security.auth import CurrentUser, ReportAuthor
from app.services import audit
from app.services.calibration import is_blinded
from app.services.http import attachment
from app.services.reports.data import gather
from app.services.reports.pdf import GENERATOR_VERSION, render_category, render_individual
from app.services.storage import LocalStorage, get_storage

router = APIRouter(prefix="/api/v1", tags=["reports"])
log = logging.getLogger("wob.reports")


class ReportIn(BaseModel):
    kind: ReportKind
    category_id: uuid.UUID
    finalist_id: uuid.UUID | None = None
    snapshot_id: uuid.UUID | None = None  # default: the category's latest ranking


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ReportKind
    category_id: uuid.UUID
    finalist_category_id: uuid.UUID | None
    finalist_name: str | None = None
    snapshot_id: uuid.UUID
    status: JobStatus
    sha256_hash: str | None
    size_bytes: int | None
    generator_version: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


def _out(session: Session, report: Report) -> ReportOut:
    out = ReportOut.model_validate(report)
    if report.finalist_category_id:
        out.finalist_name = session.scalar(
            select(Finalist.full_name)
            .join(FinalistCategory, FinalistCategory.finalist_id == Finalist.id)
            .where(FinalistCategory.id == report.finalist_category_id)
        )
    return out


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def create_report(
    body: ReportIn,
    session: SessionDep,
    storage: Annotated[LocalStorage, Depends(get_storage)],
    user: ReportAuthor,
) -> ReportOut:
    """Generate an individual or category PDF from a ranking snapshot."""
    _get_category(session, body.category_id)
    if body.snapshot_id:
        snapshot = session.get(RankingSnapshot, body.snapshot_id)
    else:
        snapshot = session.scalar(
            select(RankingSnapshot)
            .where(RankingSnapshot.category_id == body.category_id)
            .order_by(RankingSnapshot.created_at.desc())
            .limit(1)
        )
    if snapshot is None or snapshot.category_id != body.category_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "calculate a ranking for this category first")

    fc_id = None
    if body.kind == ReportKind.INDIVIDUAL:
        if body.finalist_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "finalist_id is required for an individual report",
            )
        fc_id = _get_finalist_category(session, body.category_id, body.finalist_id).id
        if fc_id not in {e.finalist_category_id for e in snapshot.entries}:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "this finalist is not in the ranking; recalculate it first",
            )

    report = Report(
        id=uuid.uuid4(),
        kind=body.kind,
        category_id=body.category_id,
        finalist_category_id=fc_id,
        snapshot_id=snapshot.id,
        status=JobStatus.RUNNING,
        generator_version=GENERATOR_VERSION,
        requested_by=user.id,
    )
    try:
        data = gather(session, snapshot)
        if body.kind == ReportKind.INDIVIDUAL:
            finalist = next(f for f in data.finalists if f.finalist_category_id == fc_id)
            pdf = render_individual(data, finalist)
        else:
            pdf = render_category(data)
    except Exception as exc:
        log.exception("report generation failed")
        session.rollback()
        report.status = JobStatus.FAILED
        report.error = f"{type(exc).__name__}: {exc}"[:2000]
        report.completed_at = datetime.now(UTC)
        session.add(report)
        audit.record(session, user, "report.failed", "report", report.id, error=report.error)
        session.commit()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "the report could not be generated"
        ) from exc

    key = f"reports/{body.category_id}/{report.id}.pdf"
    storage.put(key, pdf)
    report.storage_key = key
    report.sha256_hash = hashlib.sha256(pdf).hexdigest()
    report.size_bytes = len(pdf)
    report.status = JobStatus.SUCCEEDED
    report.completed_at = datetime.now(UTC)
    session.add(report)
    audit.record(
        session,
        user,
        "report.generated",
        "report",
        report.id,
        kind=body.kind.value,
        snapshot_id=str(snapshot.id),
        sha256=report.sha256_hash,
    )
    session.commit()
    session.refresh(report)
    return _out(session, report)


@router.get("/categories/{category_id}/reports")
def list_reports(category_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> list[ReportOut]:
    _get_category(session, category_id)
    reports = session.scalars(
        select(Report).where(Report.category_id == category_id).order_by(Report.created_at.desc())
    )
    return [_out(session, r) for r in reports]


def _get_report(session: Session, report_id: uuid.UUID) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return report


@router.get("/reports/{report_id}")
def get_report(report_id: uuid.UUID, session: SessionDep, _: CurrentUser) -> ReportOut:
    return _out(session, _get_report(session, report_id))


@router.get("/reports/{report_id}/download")
def download_report(
    report_id: uuid.UUID,
    session: SessionDep,
    storage: Annotated[LocalStorage, Depends(get_storage)],
    user: CurrentUser,
) -> Response:
    report = _get_report(session, report_id)
    if is_blinded(session, user, report.category_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "hidden while you are a blind rater in an open calibration study for this category",
        )
    if report.status != JobStatus.SUCCEEDED or not report.storage_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "this report has no file")
    audit.record(session, user, "report.downloaded", "report", report.id)
    session.commit()
    name = "category-report" if report.kind == ReportKind.CATEGORY else "individual-report"
    return attachment(
        storage.read(report.storage_key),
        f"wob-2026-{name}-{report.created_at:%Y%m%d-%H%M}.pdf",
        "application/pdf",
    )
