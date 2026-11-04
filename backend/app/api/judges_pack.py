"""Judges' pack: each category's top three finalists from its latest ranking, with their
individual reports and documents in one place, and each judge's own scores for them."""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.knowledge_base import SessionDep
from app.models import (
    AwardCategory,
    Document,
    Finalist,
    FinalistCategory,
    JobStatus,
    ProcessingStatus,
    RankingSnapshot,
    Report,
    ReportKind,
    RubricStatus,
    RubricVersion,
    User,
)
from app.security.auth import CurrentUser
from app.services import judging
from app.services.calibration import is_blinded
from app.services.ranking import display, display_points
from app.services.scales import scale_of

router = APIRouter(prefix="/api/v1", tags=["judges pack"])

PACK_RANKS = 3
# Only files that passed the malware scan; withdrawn documents are left out too.
SCANNED = (ProcessingStatus.EXTRACTED, ProcessingStatus.CHUNKING, ProcessingStatus.INDEXED)


class PackReport(BaseModel):
    id: uuid.UUID
    created_at: datetime


class PackDocument(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    source_type: str


class PackScore(BaseModel):
    """The signed-in judge's own score; other judges' scores are never in the pack."""

    score: int
    comment: str
    updated_at: datetime


class PackFinalist(BaseModel):
    finalist_id: uuid.UUID
    full_name: str
    organisation: str | None
    position_title: str | None
    rank: int
    is_tie: bool
    score_display: str | None
    points_display: str | None
    unreviewed_criteria: list[str]
    report: PackReport | None
    documents: list[PackDocument]
    my_score: PackScore | None = None


class PackCategory(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    ranking_id: uuid.UUID | None = None
    ranked_at: datetime | None = None
    production: bool = False
    hidden_for_calibration: bool = False
    scale_high: int = 5
    judging_closed: bool = False
    category_report: PackReport | None = None
    finalists: list[PackFinalist] = []


def _latest_report(
    session: Session, snapshot_id: uuid.UUID, kind: ReportKind, fc_id: uuid.UUID | None = None
) -> PackReport | None:
    report = session.scalar(
        select(Report)
        .where(
            Report.snapshot_id == snapshot_id,
            Report.kind == kind,
            Report.status == JobStatus.SUCCEEDED,
            Report.finalist_category_id.is_(None)
            if fc_id is None
            else Report.finalist_category_id == fc_id,
        )
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    return PackReport(id=report.id, created_at=report.created_at) if report else None


def _category(session: Session, category: AwardCategory, user: User) -> PackCategory:
    out = PackCategory(
        id=category.id,
        code=category.code,
        name=category.name,
        judging_closed=category.judging_closed_at is not None,
    )
    snapshot = session.scalar(
        select(RankingSnapshot)
        .where(RankingSnapshot.category_id == category.id)
        .order_by(RankingSnapshot.created_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return out
    if is_blinded(session, user, category.id):
        out.hidden_for_calibration = True
        return out
    out.ranking_id = snapshot.id
    out.ranked_at = snapshot.created_at
    out.production = snapshot.rubric_status == RubricStatus.FROZEN
    scale = scale_of(session.get_one(RubricVersion, snapshot.rubric_version_id).scale)
    out.scale_high = scale.high
    out.category_report = _latest_report(session, snapshot.id, ReportKind.CATEGORY)

    # Ties at third place bring in everyone sharing it.
    top = [e for e in snapshot.entries if e.rank is not None and e.rank <= PACK_RANKS]
    mine = judging.own_scores(session, user, [e.finalist_category_id for e in top])
    for entry in top:
        fc = session.get_one(FinalistCategory, entry.finalist_category_id)
        finalist = session.get_one(Finalist, fc.finalist_id)
        documents = session.scalars(
            select(Document)
            .where(
                Document.finalist_category_id == fc.id,
                Document.withdrawn_at.is_(None),
                Document.processing_status.in_(SCANNED),
            )
            .order_by(Document.source_type, Document.filename)
        )
        out.finalists.append(
            PackFinalist(
                finalist_id=finalist.id,
                full_name=finalist.full_name,
                organisation=finalist.organisation,
                position_title=finalist.position_title,
                rank=entry.rank,
                is_tie=entry.is_tie,
                score_display=display(entry.score),
                points_display=display_points(entry.score, scale),
                unreviewed_criteria=entry.unreviewed_criteria,
                report=_latest_report(session, snapshot.id, ReportKind.INDIVIDUAL, fc.id),
                documents=[
                    PackDocument(
                        id=d.id,
                        filename=d.filename,
                        mime_type=d.mime_type,
                        size_bytes=d.size_bytes,
                        source_type=d.source_type.value,
                    )
                    for d in documents
                ],
                my_score=PackScore(
                    score=mine[fc.id].score,
                    comment=mine[fc.id].comment,
                    updated_at=mine[fc.id].updated_at,
                )
                if fc.id in mine
                else None,
            )
        )
    out.finalists.sort(key=lambda f: (f.rank, f.full_name))
    return out


@router.get("/judges-pack")
def judges_pack(session: SessionDep, user: CurrentUser) -> list[PackCategory]:
    """Each category's top three finalists in its latest ranking, with the individual reports
    generated from that ranking and the documents that passed scanning."""
    categories = session.scalars(
        select(AwardCategory).where(AwardCategory.hidden_at.is_(None)).order_by(AwardCategory.code)
    )
    return [_category(session, c, user) for c in categories]
