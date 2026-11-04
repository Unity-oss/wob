"""Overview endpoints for the dashboard and the cross-finalist knowledge base listing."""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.knowledge_base import SessionDep
from app.models import SourceType
from app.schemas.knowledge_base import DocumentOut
from app.security.auth import CurrentUser
from app.services.calibration import is_blinded
from app.services.ranking import display
from app.services.scales import scale_of

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

TOP_RANKED = 5
STRENGTHS = 3


class CategoryCount(BaseModel):
    id: uuid.UUID
    name: str
    finalists: int
    documents: int
    ranked: bool


class Progress(BaseModel):
    nominations: int
    documents_indexed: int
    evidence_extracted: int
    assessed: int
    claims_verified: int


class TopRanked(BaseModel):
    finalist_id: uuid.UUID
    category_id: uuid.UUID
    category_name: str
    full_name: str
    organisation: str | None
    rank: int | None
    is_tie: bool
    score_display: str | None
    scale_high: int
    strengths: list[str]
    production: bool


class DashboardOut(BaseModel):
    finalists: int
    nominations: int
    categories: int
    documents: int
    categories_ranked: int
    categories_with_finalists: int
    progress: Progress
    by_category: list[CategoryCount]
    top_ranked: list[TopRanked]
    rankings_hidden: int


_CATEGORIES = text("""
    SELECT c.id, c.name,
           (SELECT count(*) FROM finalist_categories fc WHERE fc.category_id = c.id) AS finalists,
           (SELECT count(*) FROM documents d JOIN finalist_categories fc
                ON fc.id = d.finalist_category_id
             WHERE fc.category_id = c.id AND d.withdrawn_at IS NULL) AS documents,
           EXISTS (SELECT 1 FROM ranking_snapshots s WHERE s.category_id = c.id) AS ranked
      FROM award_categories c
     WHERE c.hidden_at IS NULL
     ORDER BY c.name
""")

# Per nomination: is each pipeline stage complete?
_PROGRESS = text("""
    WITH latest_run AS (
        SELECT DISTINCT ON (category_id) id, category_id
          FROM assessment_runs WHERE status = 'succeeded'
         ORDER BY category_id, created_at DESC
    )
    SELECT
      count(*) AS nominations,
      count(*) FILTER (WHERE docs.active > 0 AND docs.in_progress = 0) AS documents_indexed,
      count(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM claim_extraction_runs r
           WHERE r.finalist_category_id = fc.id AND r.status = 'succeeded'))
          AS evidence_extracted,
      count(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM criterion_assessments a JOIN latest_run lr ON lr.id = a.run_id
           WHERE a.finalist_category_id = fc.id)) AS assessed,
      count(*) FILTER (WHERE claims.total > 0 AND claims.reviewed = claims.total)
          AS claims_verified
    FROM finalist_categories fc
    JOIN award_categories c ON c.id = fc.category_id AND c.hidden_at IS NULL
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE d.withdrawn_at IS NULL) AS active,
               count(*) FILTER (WHERE d.withdrawn_at IS NULL AND d.processing_status
                   NOT IN ('indexed', 'failed', 'rejected')) AS in_progress
          FROM documents d WHERE d.finalist_category_id = fc.id
    ) docs
    CROSS JOIN LATERAL (
        SELECT count(*) AS total,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM verification_reviews v WHERE v.claim_id = cl.id)) AS reviewed
          FROM claims cl
         WHERE cl.finalist_category_id = fc.id AND cl.superseded_at IS NULL
    ) claims
""")

_LATEST_ENTRIES = text("""
    WITH latest AS (
        SELECT DISTINCT ON (category_id) id, category_id, rubric_version_id, rubric_status
          FROM ranking_snapshots
         ORDER BY category_id, created_at DESC
    )
    SELECT l.category_id, c.name AS category_name, l.rubric_version_id, l.rubric_status,
           (SELECT scale FROM rubric_versions v WHERE v.id = l.rubric_version_id) AS scale,
           f.id AS finalist_id, f.full_name, f.organisation,
           e.rank, e.is_tie, e.score, e.criterion_ratings
      FROM latest l
      JOIN award_categories c ON c.id = l.category_id AND c.hidden_at IS NULL
      JOIN ranking_entries e ON e.snapshot_id = l.id
      JOIN finalist_categories fc ON fc.id = e.finalist_category_id
      JOIN finalists f ON f.id = fc.finalist_id
     WHERE e.score IS NOT NULL
""")


@router.get("/dashboard")
def dashboard(session: SessionDep, user: CurrentUser) -> DashboardOut:
    categories = session.execute(_CATEGORIES).mappings().all()
    progress = Progress(**session.execute(_PROGRESS).mappings().one())

    hidden = {c["id"] for c in categories if is_blinded(session, user, c["id"])}
    entries = [
        r for r in session.execute(_LATEST_ENTRIES).mappings() if r["category_id"] not in hidden
    ]
    names: dict[tuple[Any, str], str] = {
        (row.rubric_version_id, row.code): row.name
        for row in session.execute(
            text("SELECT rubric_version_id, code, name FROM rubric_criteria ORDER BY position")
        )
    }
    entries.sort(key=lambda r: r["score"], reverse=True)
    top = []
    for r in entries[:TOP_RANKED]:
        scale = scale_of(r["scale"])
        # Strengths: the scale's top two levels (4-5 on 1-5, Good and Excellent on 0-3).
        ratings = sorted(
            (
                (v, k)
                for k, v in (r["criterion_ratings"] or {}).items()
                if v is not None and v >= scale.high - 1
            ),
            reverse=True,
        )
        top.append(
            TopRanked(
                finalist_id=r["finalist_id"],
                category_id=r["category_id"],
                category_name=r["category_name"],
                full_name=r["full_name"],
                organisation=r["organisation"],
                rank=r["rank"],
                is_tie=r["is_tie"],
                score_display=display(r["score"]),
                scale_high=scale.high,
                strengths=[
                    names.get((r["rubric_version_id"], k), k) for _, k in ratings[:STRENGTHS]
                ],
                production=r["rubric_status"] == "frozen",
            )
        )

    return DashboardOut(
        finalists=session.scalar(text("SELECT count(*) FROM finalists")) or 0,
        nominations=progress.nominations,
        categories=len(categories),
        documents=sum(c["documents"] for c in categories),
        categories_ranked=sum(1 for c in categories if c["ranked"] and c["finalists"]),
        categories_with_finalists=sum(1 for c in categories if c["finalists"]),
        progress=progress,
        by_category=[CategoryCount(**c) for c in categories],
        top_ranked=top,
        rankings_hidden=len(hidden),
    )


class LibraryDocument(DocumentOut):
    finalist_id: uuid.UUID
    finalist_name: str
    category_id: uuid.UUID
    category_name: str


class LibraryOut(BaseModel):
    total: int
    items: list[LibraryDocument]


@router.get("/documents")
def library(
    session: SessionDep,
    _: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    category_id: uuid.UUID | None = None,
    finalist_id: uuid.UUID | None = None,
    source_type: SourceType | None = None,
    kind: Annotated[str | None, Query(pattern="^(pdf|word|spreadsheet|image|text|other)$")] = None,
    added_from: date | None = None,
    added_to: date | None = None,
    include_withdrawn: bool = False,
    limit: Annotated[int, Query(gt=0, le=500)] = 200,
) -> LibraryOut:
    """Every document across all finalists, filtered. `q` matches file and finalist names."""
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if q:
        where.append("(d.filename ILIKE :q OR f.full_name ILIKE :q)")
        params["q"] = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
    if category_id:
        where.append("fc.category_id = :category_id")
        params["category_id"] = category_id
    if finalist_id:
        where.append("f.id = :finalist_id")
        params["finalist_id"] = finalist_id
    if source_type:
        where.append("d.source_type = :source_type")
        params["source_type"] = source_type.value
    if kind:
        where.append(f"({_KIND_SQL}) = :kind")
        params["kind"] = kind
    if added_from:
        where.append("d.uploaded_at >= :added_from")
        params["added_from"] = datetime.combine(added_from, time.min)
    if added_to:
        where.append("d.uploaded_at <= :added_to")
        params["added_to"] = datetime.combine(added_to, time.max)
    if not include_withdrawn:
        where.append("d.withdrawn_at IS NULL")
    base = (
        "FROM documents d JOIN finalist_categories fc ON fc.id = d.finalist_category_id "
        "JOIN finalists f ON f.id = fc.finalist_id "
        "JOIN award_categories c ON c.id = fc.category_id AND c.hidden_at IS NULL "
        "WHERE " + " AND ".join(where)
    )
    total = session.scalar(text(f"SELECT count(*) {base}"), params) or 0  # noqa: S608
    rows = session.execute(
        text(
            "SELECT d.*, f.id AS finalist_id, f.full_name AS finalist_name, "  # noqa: S608
            f"c.id AS category_id, c.name AS category_name {base} "
            "ORDER BY d.uploaded_at DESC, d.filename LIMIT :limit"
        ),
        params,
    ).mappings()
    return LibraryOut(total=total, items=[LibraryDocument.model_validate(dict(r)) for r in rows])


_KIND_SQL = """CASE
    WHEN d.mime_type = 'application/pdf' THEN 'pdf'
    WHEN d.mime_type LIKE '%wordprocessingml%' OR d.mime_type = 'application/msword' THEN 'word'
    WHEN d.mime_type LIKE '%spreadsheetml%' OR d.mime_type = 'text/csv' THEN 'spreadsheet'
    WHEN d.mime_type LIKE 'image/%' THEN 'image'
    WHEN d.mime_type LIKE 'text/%' THEN 'text'
    ELSE 'other' END"""
