"""Subject-access export (Act s.24; Reg 35): everything held about one nomination, as a ZIP.

Contents: the finalist's documents (decrypted), individual reports, and data.json with their
consents, claims, reviews, public-source findings, ratings, rankings, calibration ratings and
an access log. Other individuals are not identified (s.24(4)): reviewers, raters and staff
appear by role only. Recipients are listed by category (s.24(1)(c)).
"""

import io
import json
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    AwardCategory,
    Claim,
    ClaimEvidence,
    CriterionAssessment,
    Document,
    ExternalSource,
    Finalist,
    FinalistCategory,
    FinalistConsent,
    JudgeScore,
    RankingEntry,
    RankingSnapshot,
    ReferenceNote,
    Report,
    ReportKind,
    RubricCriterion,
    User,
    VerificationReview,
)
from app.services.storage import LocalStorage

README = """Personal data held about you by the Women on Boards Awards 2026 evidence system

This archive answers a request under section 24 of Uganda's Data Protection and Privacy Act,
2019. It contains:

  data.json      everything recorded about your nomination: consents, the factual claims
                 drawn from your evidence and how they were verified, public sources found,
                 criterion ratings (AI-proposed and as reviewed), rankings, and who had access
                 (by role)
  documents/     the documents submitted for your nomination
  reports/       individual deliberation reports about you

Other people (reviewers, raters, staff) are identified by role only, to protect their
privacy (s.24(4)). You may ask for corrections or deletion (s.16) or object to processing
(s.25) by contacting the data protection officer.
"""

PROCESSORS = {
    "ai": "Anthropic PBC (United States): AI-assisted claim extraction and assessment",
    "research": "Anthropic PBC (United States) and web search providers: public-source research",
}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def build_export(
    session: Session, storage: LocalStorage, finalist_category_id: uuid.UUID
) -> tuple[bytes, str]:
    fc = session.get_one(FinalistCategory, finalist_category_id)
    finalist = session.get_one(Finalist, fc.finalist_id)
    category = session.get_one(AwardCategory, fc.category_id)
    roles = {u.id: u.role.value for u in session.scalars(select(User))}

    documents = session.scalars(
        select(Document)
        .where(Document.finalist_category_id == fc.id)
        .order_by(Document.uploaded_at)
    ).all()
    doc_names = {d.id: d.filename for d in documents}
    chunk_docs = dict(
        session.execute(
            text("SELECT id, document_id FROM document_chunks WHERE finalist_category_id = :fc"),
            {"fc": fc.id},
        ).all()
    )

    claims = []
    for claim in session.scalars(select(Claim).where(Claim.finalist_category_id == fc.id)):
        evidence = session.scalars(select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id))
        reviews = session.scalars(
            select(VerificationReview)
            .where(VerificationReview.claim_id == claim.id)
            .order_by(VerificationReview.created_at)
        )
        claims.append(
            {
                "statement": claim.wording,
                "type": claim.claim_type,
                "status": claim.status.value,
                "period": claim.period_text,
                "current": claim.superseded_at is None,
                "evidence": [
                    {
                        "relation": e.relation.value,
                        "quote": e.quoted_span,
                        "source": doc_names.get(chunk_docs.get(e.chunk_id))
                        if e.chunk_id
                        else "public source",
                    }
                    for e in evidence
                ],
                "reviews": [
                    {
                        "decision": r.decision.value,
                        "notes": r.notes,
                        "by_role": roles.get(r.reviewer_id) if r.reviewer_id else "automatic",
                        "at": _iso(r.created_at),
                    }
                    for r in reviews
                ],
            }
        )

    sources = [
        {
            "publisher": s.publisher,
            "url": s.url,
            "criterion": s.criterion_name,
            "quote": s.quote,
            "outcome": s.reviewer_outcome.value,
            "retrieved_at": _iso(s.retrieved_at),
        }
        for s in session.scalars(
            select(ExternalSource).where(ExternalSource.finalist_category_id == fc.id)
        )
    ]

    criteria = {c.id: c.name for c in session.scalars(select(RubricCriterion))}
    assessments = [
        {
            "criterion": criteria.get(a.criterion_id),
            "source": a.source,
            "proposed_rating": a.rating,
            "reviewer_rating": a.reviewer_rating,
            "rating": a.effective_rating,
            "rationale": a.rationale,
            "limitations": a.limitations,
            "status": a.status.value,
            "review_notes": a.review_notes,
            "reviewed_by_role": roles.get(a.reviewed_by),
            "citations": [c.quoted_span for c in a.citations],
            "at": _iso(a.created_at),
        }
        for a in session.scalars(
            select(CriterionAssessment)
            .where(CriterionAssessment.finalist_category_id == fc.id)
            .order_by(CriterionAssessment.created_at)
        )
    ]
    rankings = [
        {
            "calculated_at": _iso(snapshot.created_at),
            "rubric_status": snapshot.rubric_status.value,
            "score": str(entry.score) if entry.score is not None else None,
            "rank": entry.rank,
            "tie": entry.is_tie,
            "ratings": entry.criterion_ratings,
        }
        for entry, snapshot in session.execute(
            select(RankingEntry, RankingSnapshot)
            .join(RankingSnapshot, RankingSnapshot.id == RankingEntry.snapshot_id)
            .where(RankingEntry.finalist_category_id == fc.id)
            .order_by(RankingSnapshot.created_at)
        )
    ]
    calibration = [
        {"criterion": row.criterion, "rating": row.rating, "by_role": roles.get(row.rater_id)}
        for row in session.execute(
            text(
                "SELECT c.name AS criterion, r.rating, r.rater_id FROM calibration_ratings r "
                "JOIN calibration_items i ON i.id = r.item_id "
                "JOIN rubric_criteria c ON c.id = i.criterion_id WHERE i.finalist_category_id = :fc"
            ),
            {"fc": fc.id},
        )
    ]
    consents = [
        {
            "notice_version": c.notice_version,
            "assessment": c.assessment,
            "ai_processing": c.ai_processing,
            "web_research": c.web_research,
            "method": c.method,
            "recorded_at": _iso(c.recorded_at),
        }
        for c in session.scalars(
            select(FinalistConsent)
            .where(FinalistConsent.finalist_category_id == fc.id)
            .order_by(FinalistConsent.recorded_at)
        )
    ]

    related = [fc.id, *doc_names] + list(
        session.scalars(
            text(
                "SELECT id FROM claims WHERE finalist_category_id = :fc UNION ALL "
                "SELECT id FROM external_sources WHERE finalist_category_id = :fc UNION ALL "
                "SELECT id FROM criterion_assessments WHERE finalist_category_id = :fc"
            ),
            {"fc": fc.id},
        )
    )
    events = session.execute(
        text(
            "SELECT action, actor_id, occurred_at FROM audit_events "
            "WHERE entity_id = ANY(:ids) ORDER BY id"
        ),
        {"ids": related},
    ).all()
    access_log = [
        {"action": e.action, "by_role": roles.get(e.actor_id, "system"), "at": _iso(e.occurred_at)}
        for e in events
    ]
    used_ai = any(a["source"] == "ai" for a in assessments) or bool(claims)
    recipients = {
        "roles_with_access": sorted({"secretariat", "reviewer", "judge", "committee", "admin"}),
        "accessed_by_role": dict(Counter(e["by_role"] for e in access_log)),
        "processors": [PROCESSORS["ai"]] * used_ai + [PROCESSORS["research"]] * bool(sources),
    }

    data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "finalist": {
            "name": finalist.full_name,
            "organisation": finalist.organisation,
            "position": finalist.position_title,
        },
        "nomination": {
            "category": category.name,
            "added_at": _iso(fc.created_at),
            "eligibility_flagged": fc.eligibility_flagged,
        },
        "consents": consents,
        "reference_notes": [
            {"referee": r.referee_name, "notes": r.notes, "recorded_at": _iso(r.updated_at)}
            for r in session.scalars(
                select(ReferenceNote)
                .where(ReferenceNote.finalist_category_id == fc.id)
                .order_by(ReferenceNote.position)
            )
        ],
        # Judges are identified by role only, like other people in this export.
        "judge_scores": [
            {
                "by_role": "judge",
                "score": s.score,
                "comment": s.comment,
                "recorded_at": _iso(s.updated_at),
            }
            for s in session.scalars(
                select(JudgeScore)
                .where(JudgeScore.finalist_category_id == fc.id)
                .order_by(JudgeScore.created_at)
            )
        ],
        "documents": [
            {
                "filename": d.filename,
                "uploaded_at": _iso(d.uploaded_at),
                "sha256": d.sha256_hash,
                "status": d.processing_status.value,
                "withdrawn": d.withdrawn_at is not None,
                "withdrawal_reason": d.withdrawal_reason,
            }
            for d in documents
        ],
        "claims": claims,
        "public_sources": sources,
        "criterion_assessments": assessments,
        "rankings": rankings,
        "calibration_ratings": calibration,
        "recipients": recipients,
        "access_log": access_log,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README)
        zf.writestr("data.json", json.dumps(data, indent=2, ensure_ascii=False))
        for n, d in enumerate(documents, start=1):
            zf.writestr(f"documents/{n:02d}-{d.filename}", storage.read(d.storage_key))
        for report in session.scalars(
            select(Report).where(
                Report.finalist_category_id == fc.id,
                Report.kind == ReportKind.INDIVIDUAL,
                Report.storage_key.is_not(None),
            )
        ):
            zf.writestr(
                f"reports/{report.created_at:%Y%m%d-%H%M}-individual-report.pdf",
                storage.read(report.storage_key),
            )
    safe = "".join(ch if ch.isalnum() else "-" for ch in finalist.full_name).strip("-").lower()
    return buffer.getvalue(), f"subject-access-{safe or 'finalist'}-{datetime.now(UTC):%Y%m%d}.zip"
