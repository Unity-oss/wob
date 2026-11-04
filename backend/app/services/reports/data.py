"""Everything a deliberation report shows, gathered from one ranking snapshot (spec §11).

Numbers (scores, ranks, criterion ratings) come from the immutable snapshot, so a report is
reproducible. Narrative detail (rationales, citations, reviews, claim statuses) is read as it
stands when the report is generated; where a rating was reviewed after the snapshot, the report
says so rather than silently mixing the two.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import (
    AssessmentRun,
    AwardCategory,
    Claim,
    ClaimExtractionRun,
    ContradictionResolution,
    CriterionAssessment,
    Document,
    ExternalSource,
    Finalist,
    FinalistCategory,
    JobStatus,
    RankingEntry,
    RankingSnapshot,
    ReviewOutcome,
    RubricCriterion,
    RubricStatus,
    RubricVersion,
    User,
)
from app.services.calibration import latest_result
from app.services.ranking import display, display_points
from app.services.reports.questions import category_questions, finalist_questions
from app.services.scales import Scale, scale_of

DISCLAIMER = (
    "AI rankings are advisory. The awards committee retains responsibility for final "
    "deliberation and winner approval."
)


@dataclass
class CitationLine:
    relation: str
    quote: str
    source: str


@dataclass
class CriterionResult:
    code: str
    name: str
    weight: Decimal | None
    snapshot_rating: int | None
    ai_rating: int | None = None
    reviewer_rating: int | None = None
    current_rating: int | None = None
    status: str = "not assessed"
    sufficiency: str | None = None
    rationale: str | None = None
    limitations: str | None = None
    flags: list[str] = field(default_factory=list)
    review_notes: str | None = None
    reviewed_by: str | None = None
    citations: list[CitationLine] = field(default_factory=list)

    @property
    def changed_since_snapshot(self) -> bool:
        return self.current_rating != self.snapshot_rating


@dataclass
class DocumentLine:
    filename: str
    sha256: str
    uploaded_at: datetime
    status: str
    withdrawn: bool


@dataclass
class FinalistReport:
    finalist_category_id: uuid.UUID
    name: str
    organisation: str | None
    position: str | None
    rank: int | None
    is_tie: bool
    tied_with: list[str]
    score_display: str | None
    points_display: str | None
    criteria_assessed: int
    criteria_total: int
    unreviewed: list[str]
    criteria: list[CriterionResult]
    claim_counts: dict[str, int]
    unresolved_claims: list[tuple[str, str]]
    unresolved_contradictions: list[str]
    accepted_public_sources: int
    documents: list[DocumentLine]
    strengths: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class Spread:
    criterion: str
    highest: list[str]
    high: int
    lowest: list[str]
    low: int

    @property
    def size(self) -> int:
        return self.high - self.low


@dataclass
class ReportData:
    category: str
    snapshot_id: uuid.UUID
    snapshot_at: datetime
    calculation_version: str
    weighting: str
    scale: Scale
    rubric_version: int
    rubric_status: RubricStatus
    run: dict[str, object]
    criteria: list[tuple[str, str, Decimal | None]]
    finalists: list[FinalistReport]
    close_results: list[tuple[str, str, str]]
    spreads: list[Spread]
    questions: list[str]
    generated_at: datetime
    disclaimer: str = DISCLAIMER

    @property
    def production(self) -> bool:
        return self.rubric_status == RubricStatus.FROZEN


def _citations(session: Session, assessment: CriterionAssessment) -> list[CitationLine]:
    chunks = {
        row.id: row
        for row in session.execute(
            text(
                "SELECT c.id, c.page_number, d.filename FROM document_chunks c "
                "JOIN documents d ON d.id = c.document_id WHERE c.id = ANY(:ids)"
            ),
            {"ids": [c.chunk_id for c in assessment.citations if c.chunk_id]},
        )
    }
    lines = []
    for c in assessment.citations:
        if c.chunk_id and c.chunk_id in chunks:
            row = chunks[c.chunk_id]
            source = row.filename + (f", p. {row.page_number}" if row.page_number else "")
        elif c.external_source_id:
            public = session.get(ExternalSource, c.external_source_id)
            source = f"{public.publisher} ({public.url})" if public else "public source"
        else:
            source = "source unavailable"
        lines.append(CitationLine(c.relation.value, c.quoted_span, source))
    return lines


def _finalist(
    session: Session,
    entry: RankingEntry,
    criteria: list[RubricCriterion],
    run: AssessmentRun,
    tied_with: list[str],
    scale: Scale,
) -> FinalistReport:
    fc = session.get_one(FinalistCategory, entry.finalist_category_id)
    finalist = session.get_one(Finalist, fc.finalist_id)
    assessments = {
        a.criterion_id: a
        for a in session.scalars(
            select(CriterionAssessment).where(
                CriterionAssessment.run_id == run.id,
                CriterionAssessment.finalist_category_id == fc.id,
            )
        )
    }
    results = []
    for c in criteria:
        result = CriterionResult(c.code, c.name, c.weight, entry.criterion_ratings.get(c.code))
        a = assessments.get(c.id)
        if a:
            reviewer = session.get(User, a.reviewed_by) if a.reviewed_by else None
            result.ai_rating = a.rating
            result.reviewer_rating = a.reviewer_rating
            result.current_rating = None if a.status.value == "rejected" else a.effective_rating
            result.status = a.status.value
            result.sufficiency = a.sufficiency.value
            result.rationale = a.rationale
            result.limitations = a.limitations
            result.flags = list(a.verification_flags)
            result.review_notes = a.review_notes
            result.reviewed_by = reviewer.display_name if reviewer else None
            result.citations = _citations(session, a)
        results.append(result)

    claims = session.scalars(
        select(Claim).where(Claim.finalist_category_id == fc.id, Claim.superseded_at.is_(None))
    ).all()
    counts = Counter(c.status.value for c in claims)
    unresolved_claims = [
        (c.wording, c.status.value)
        for c in claims
        if c.status.value in ("disputed", "missing", "unverified")
    ]

    contradictions: list[str] = []
    extraction = session.scalar(
        select(ClaimExtractionRun)
        .where(
            ClaimExtractionRun.finalist_category_id == fc.id,
            ClaimExtractionRun.status == JobStatus.SUCCEEDED,
        )
        .order_by(ClaimExtractionRun.created_at.desc())
        .limit(1)
    )
    if extraction:
        resolved = set(
            session.scalars(
                select(ContradictionResolution.contradiction_index).where(
                    ContradictionResolution.extraction_run_id == extraction.id
                )
            )
        )
        contradictions = [
            c["description"] for i, c in enumerate(extraction.contradictions) if i not in resolved
        ]

    public = session.scalar(
        select(func.count(ExternalSource.id)).where(
            ExternalSource.finalist_category_id == fc.id,
            ExternalSource.reviewer_outcome == ReviewOutcome.ACCEPTED,
        )
    )
    documents = [
        DocumentLine(
            d.filename,
            d.sha256_hash,
            d.uploaded_at,
            d.processing_status.value,
            d.withdrawn_at is not None,
        )
        for d in session.scalars(
            select(Document)
            .where(Document.finalist_category_id == fc.id)
            .order_by(Document.uploaded_at, Document.filename)
        )
    ]
    report = FinalistReport(
        finalist_category_id=fc.id,
        name=finalist.full_name,
        organisation=finalist.organisation,
        position=finalist.position_title,
        rank=entry.rank,
        is_tie=entry.is_tie,
        tied_with=tied_with,
        score_display=display(entry.score),
        points_display=display_points(entry.score, scale),
        criteria_assessed=entry.criteria_assessed,
        criteria_total=entry.criteria_total,
        unreviewed=list(entry.unreviewed_criteria),
        criteria=results,
        claim_counts=dict(counts),
        unresolved_claims=unresolved_claims,
        unresolved_contradictions=contradictions,
        accepted_public_sources=public or 0,
        documents=documents,
    )
    # Strengths: the top two levels on 1-5 (4 and 5), the top two on 0-3 (Good, Excellent).
    report.strengths = [
        f"{r.name} ({r.snapshot_rating}/{scale.high})"
        + (f": “{_first_support(r)}” ({r.citations[0].source})" if _first_support(r) else "")
        for r in sorted(results, key=lambda r: -(r.snapshot_rating or 0))
        if r.snapshot_rating is not None and r.snapshot_rating >= scale.high - 1
    ]
    report.limitations = [
        f"{r.name} ({r.snapshot_rating if r.snapshot_rating is not None else 'not rated'}"
        f"{f'/{scale.high}' if r.snapshot_rating is not None else ''}): "
        + (r.limitations or f"evidence {r.sufficiency or 'not assessed'}")
        for r in results
        if r.snapshot_rating is None
        or r.snapshot_rating <= 2
        or r.sufficiency in ("limited", "insufficient")
    ]
    report.questions = finalist_questions(report)
    return report


def _first_support(result: CriterionResult) -> str | None:
    return next((c.quote for c in result.citations if c.relation == "supports"), None)


def _calibration_line(session: Session, snapshot: RankingSnapshot) -> str:
    study = latest_result(session, snapshot.category_id)
    if study is None:
        return "not calibrated: no closed calibration study for this category (FR-15)"
    same = "this assessment run" if study.run_id == snapshot.run_id else "an earlier assessment run"
    return f"{study.outcome} ({study.name}, {study.closed_at:%d %b %Y}, on {same})"


def gather(session: Session, snapshot: RankingSnapshot) -> ReportData:
    run = session.get_one(AssessmentRun, snapshot.run_id)
    rubric = session.get_one(RubricVersion, snapshot.rubric_version_id)
    scale = scale_of(rubric.scale)
    category = session.get_one(AwardCategory, snapshot.category_id)
    criteria = list(rubric.criteria)
    names = dict(
        session.execute(
            select(FinalistCategory.id, Finalist.full_name)
            .join(Finalist, Finalist.id == FinalistCategory.finalist_id)
            .where(FinalistCategory.id.in_([e.finalist_category_id for e in snapshot.entries]))
        ).all()
    )
    entries = sorted(
        snapshot.entries,
        key=lambda e: (e.rank is None, e.rank or 0, names[e.finalist_category_id]),
    )
    finalists = [
        _finalist(
            session,
            e,
            criteria,
            run,
            [
                names[o.finalist_category_id]
                for o in entries
                if e.is_tie and o is not e and o.score == e.score
            ],
            scale,
        )
        for e in entries
    ]

    scored = [e for e in entries if e.score is not None]
    close = [
        (names[a.finalist_category_id], names[b.finalist_category_id], display(a.score - b.score))
        for a, b in zip(scored, scored[1:], strict=False)
        if a.score - b.score <= Decimal("0.25") * (scale.high - scale.low) / 4
    ]
    spreads = []
    for c in criteria:
        rated = [
            (f.name, r.snapshot_rating)
            for f in finalists
            for r in f.criteria
            if r.code == c.code and r.snapshot_rating is not None
        ]
        if len(rated) >= 2:
            high = max(v for _, v in rated)
            low = min(v for _, v in rated)
            spreads.append(
                Spread(
                    c.name,
                    [n for n, v in rated if v == high],
                    high,
                    [n for n, v in rated if v == low],
                    low,
                )
            )
    spreads.sort(key=lambda s: -s.size)

    data = ReportData(
        category=category.name,
        snapshot_id=snapshot.id,
        snapshot_at=snapshot.created_at,
        calculation_version=snapshot.calculation_version,
        weighting=snapshot.weighting.value,
        scale=scale,
        rubric_version=rubric.version,
        rubric_status=snapshot.rubric_status,
        run={
            "id": str(run.id),
            "completed_at": run.completed_at,
            "models": run.llm_model
            + (f" (served by {', '.join(run.served_by_models)})" if run.served_by_models else ""),
            "prompt_version": run.prompt_version,
            "embedding_model": run.embedding_model,
            "reranker_model": run.reranker_model,
            "retrieval": run.retrieval_config,
            "calibration": _calibration_line(session, snapshot),
        },
        criteria=[(c.code, c.name, c.weight) for c in criteria],
        finalists=finalists,
        close_results=close,
        spreads=spreads,
        questions=[],
        generated_at=datetime.now(UTC),
    )
    data.questions = category_questions(data)
    return data
