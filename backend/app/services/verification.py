"""Reviewer verification of extracted claims (FR-07, spec §6).

Statuses: verified (checked against the original evidence), corroborated (supported by
independent documents), disputed, missing (the evidence the claim relies on is not provided),
unverified (not yet reviewed, or reset). Every decision is an append-only review row; the
claim's status is the latest decision.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.models import (
    Claim,
    ClaimEvidence,
    ClaimStatus,
    EvidenceRelation,
    EvidenceSufficiency,
    ExternalSource,
    QualityLevel,
    ReviewOutcome,
    User,
    VerificationReview,
)
from app.services import audit
from app.services.citations import normalise

NOTES_REQUIRED = {
    ClaimStatus.DISPUTED,
    ClaimStatus.MISSING,
    ClaimStatus.UNVERIFIED,
    ClaimStatus.CORROBORATED,
}


class ReviewRejected(ValueError):
    """The decision is not allowed; `conflict` means state changed, otherwise invalid input."""

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


def independent_sources(session: Session, claim_id: uuid.UUID) -> int:
    """Independent support for a claim: distinct non-withdrawn documents plus distinct
    websites of accepted public sources."""
    documents = session.execute(
        text(
            "SELECT count(DISTINCT c.document_id) FROM claim_evidence e "
            "JOIN document_chunks c ON c.id = e.chunk_id "
            "JOIN documents d ON d.id = c.document_id "
            "WHERE e.claim_id = :claim AND e.relation = 'supports' AND d.withdrawn_at IS NULL"
        ),
        {"claim": claim_id},
    ).scalar_one()
    urls = session.scalars(
        text(
            "SELECT s.url FROM claim_evidence e "
            "JOIN external_sources s ON s.id = e.external_source_id "
            "WHERE e.claim_id = :claim AND e.relation = 'supports' "
            "AND s.reviewer_outcome = 'accepted'"
        ),
        {"claim": claim_id},
    )
    websites = {
        urlsplit(url).hostname.removeprefix("www.") for url in urls if urlsplit(url).hostname
    }
    return documents + len(websites)


def review_claim(
    session: Session,
    claim: Claim,
    reviewer: User,
    decision: ClaimStatus,
    *,
    expected_status: ClaimStatus,
    notes: str | None = None,
    sufficiency: EvidenceSufficiency | None = None,
    credibility: QualityLevel | None = None,
) -> VerificationReview:
    notes = (notes or "").strip() or None
    if claim.superseded_at is not None:
        raise ReviewRejected("this claim was replaced by a later extraction", conflict=True)
    if claim.status != expected_status:
        raise ReviewRejected(
            f"the claim is now {claim.status.value}; reload and review again", conflict=True
        )
    if decision in NOTES_REQUIRED and not notes:
        raise ReviewRejected(f"notes are required to mark a claim {decision.value}")

    sources = independent_sources(session, claim.id)
    if decision == ClaimStatus.VERIFIED and sources < 1:
        raise ReviewRejected(
            "no supporting document or accepted public source remains; it cannot be verified",
            conflict=True,
        )
    if decision == ClaimStatus.CORROBORATED and sources < 2:
        raise ReviewRejected(
            "corroboration needs at least two independent sources (documents or accepted "
            "public sources); this claim has one. Mark it verified, or add a corroborating "
            "source"
        )

    review = VerificationReview(
        id=uuid.uuid4(),
        claim_id=claim.id,
        reviewer_id=reviewer.id,
        previous_status=claim.status,
        decision=decision,
        notes=notes,
        sufficiency=sufficiency,
        credibility=credibility,
    )
    session.add(review)
    claim.status = decision
    audit.record(
        session,
        reviewer,
        "claim.reviewed",
        "claim",
        claim.id,
        previous_status=review.previous_status.value,
        decision=decision.value,
        notes=notes,
        sufficiency=sufficiency.value if sufficiency else None,
        credibility=credibility.value if credibility else None,
    )
    return review


JURY_BASIS = "jury_supporting_documents"
JURY_NOTE = (
    "Verified automatically: the jury decided on 23 October 2026 that supporting documents are "
    "a credible source of truth, and a supporting document supports this claim."
)

# Current, never-reviewed, unverified claims of a candidate that a passage of a non-withdrawn
# supporting document supports and no evidence contradicts.
_JURY_VERIFIABLE = text(
    """
    SELECT cl.id FROM claims cl
    WHERE cl.finalist_category_id = :fc
      AND cl.superseded_at IS NULL
      AND cl.status = 'unverified'
      AND NOT EXISTS (SELECT 1 FROM verification_reviews v WHERE v.claim_id = cl.id)
      AND NOT EXISTS (
          SELECT 1 FROM claim_evidence e WHERE e.claim_id = cl.id AND e.relation = 'contradicts')
      AND EXISTS (
          SELECT 1 FROM claim_evidence e
          JOIN document_chunks c ON c.id = e.chunk_id
          JOIN documents d ON d.id = c.document_id
          WHERE e.claim_id = cl.id AND e.relation = 'supports'
            AND d.source_type = 'supporting_document' AND d.withdrawn_at IS NULL)
    ORDER BY cl.created_at, cl.id
    """
)


def verify_from_supporting_documents(session: Session, finalist_category_id: uuid.UUID) -> int:
    """Apply the jury decision to one candidate: verify each claim a supporting document
    supports, unless a reviewer has already decided on it or evidence contradicts it. Returns how
    many were verified; the caller commits."""
    verified = 0
    for claim_id in session.scalars(_JURY_VERIFIABLE, {"fc": finalist_category_id}).all():
        claim = session.get_one(Claim, claim_id)
        session.add(
            VerificationReview(
                id=uuid.uuid4(),
                claim_id=claim.id,
                reviewer_id=None,
                basis=JURY_BASIS,
                previous_status=claim.status,
                decision=ClaimStatus.VERIFIED,
                notes=JURY_NOTE,
            )
        )
        claim.status = ClaimStatus.VERIFIED
        audit.record(
            session, None, "claim.verified_automatically", "claim", claim.id, basis=JURY_BASIS
        )
        verified += 1
    return verified


def latest_reviews(
    session: Session, claim_ids: list[uuid.UUID]
) -> dict[uuid.UUID, VerificationReview]:
    if not claim_ids:
        return {}
    newest = (
        select(VerificationReview.claim_id, func.max(VerificationReview.created_at).label("at"))
        .where(VerificationReview.claim_id.in_(claim_ids))
        .group_by(VerificationReview.claim_id)
        .subquery()
    )
    rows = session.scalars(
        select(VerificationReview).join(
            newest,
            (VerificationReview.claim_id == newest.c.claim_id)
            & (VerificationReview.created_at == newest.c.at),
        )
    )
    return {review.claim_id: review for review in rows}


@dataclass(frozen=True)
class PreviousReview:
    review: VerificationReview
    claim_id: uuid.UUID


def reviewed_claims_by_wording(
    session: Session, finalist_category_id: uuid.UUID
) -> dict[str, PreviousReview]:
    """Current claims that a human has reviewed, keyed by normalised wording."""
    claims = session.scalars(
        select(Claim).where(
            Claim.finalist_category_id == finalist_category_id, Claim.superseded_at.is_(None)
        )
    ).all()
    reviews = latest_reviews(session, [c.id for c in claims])
    return {
        normalise(c.wording): PreviousReview(reviews[c.id], c.id) for c in claims if c.id in reviews
    }


def carry_forward(
    session: Session, claim: Claim, previous: PreviousReview, contradicted: bool
) -> bool:
    """Re-apply a human review to an identical claim from a new extraction, if still valid."""
    old = previous.review
    if contradicted or old.decision == ClaimStatus.UNVERIFIED:
        return False
    sources = independent_sources(session, claim.id)
    if old.decision == ClaimStatus.VERIFIED and sources < 1:
        return False
    if old.decision == ClaimStatus.CORROBORATED and sources < 2:
        return False
    note = (
        f"Carried forward from the review of {old.created_at:%d %b %Y} on the previous extraction."
    )
    if old.notes:
        note += f" Original notes: {old.notes}"
    session.add(
        VerificationReview(
            id=uuid.uuid4(),
            claim_id=claim.id,
            reviewer_id=old.reviewer_id,
            basis=old.basis,
            previous_status=claim.status,
            decision=old.decision,
            notes=note,
            sufficiency=old.sufficiency,
            credibility=old.credibility,
            carried_from=old.id,
        )
    )
    claim.status = old.decision
    return True


def external_evidence_by_wording(
    session: Session, finalist_category_id: uuid.UUID
) -> dict[str, list[tuple[uuid.UUID, EvidenceRelation, str]]]:
    """Public-source evidence on current claims, keyed by normalised claim wording."""
    rows = session.execute(
        select(
            Claim.wording,
            ClaimEvidence.external_source_id,
            ClaimEvidence.relation,
            ClaimEvidence.quoted_span,
        )
        .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
        .where(
            Claim.finalist_category_id == finalist_category_id,
            Claim.superseded_at.is_(None),
            ClaimEvidence.external_source_id.is_not(None),
        )
    )
    evidence: dict[str, list[tuple[uuid.UUID, EvidenceRelation, str]]] = {}
    for wording, source_id, relation, quote in rows:
        evidence.setdefault(normalise(wording), []).append((source_id, relation, quote))
    return evidence


class SourceReviewRejected(ReviewRejected):
    pass


# Source types trusted enough for a supporting finding to be accepted without a reviewer, and the
# credibility recorded for them. News and other sites always wait for a reviewer.
AUTO_ACCEPT_CREDIBILITY = {
    "official_registry": QualityLevel.HIGH,
    "regulator": QualityLevel.HIGH,
    "annual_report": QualityLevel.HIGH,
    "professional_body": QualityLevel.HIGH,
    "organisation_website": QualityLevel.MEDIUM,
}


def _attach_to_claim(session: Session, source: ExternalSource, claim: Claim | None) -> None:
    if claim is not None:  # criterion findings feed assessment directly (assess.py)
        session.add(
            ClaimEvidence(
                claim_id=claim.id,
                finalist_category_id=source.finalist_category_id,
                external_source_id=source.id,
                relation=source.relation,
                quoted_span=source.quote or source.evidence_passage,
            )
        )


def auto_accept_external_source(session: Session, source: ExternalSource) -> bool:
    """Accept a new finding without a reviewer if it supports its claim or criterion, comes from
    a credible source type, and is certainly about this nominee. Evidence against a nominee,
    possible namesakes and sources already accepted for the same subject wait for a reviewer."""
    credibility = AUTO_ACCEPT_CREDIBILITY.get(source.source_type or "")
    if (
        credibility is None
        or source.reviewer_outcome != ReviewOutcome.PENDING
        or source.superseded_at is not None
        or source.relation == EvidenceRelation.CONTRADICTS
        or source.identity_confidence != QualityLevel.HIGH
    ):
        return False
    claim = session.get(Claim, source.claim_id) if source.claim_id else None
    if source.claim_id and (claim is None or claim.superseded_at is not None):
        return False
    already = session.scalar(
        select(func.count(ExternalSource.id)).where(
            ExternalSource.finalist_category_id == source.finalist_category_id,
            ExternalSource.id != source.id,
            ExternalSource.url == source.url,
            ExternalSource.claim_id.is_not_distinct_from(source.claim_id),
            ExternalSource.criterion_code.is_not_distinct_from(source.criterion_code),
            ExternalSource.reviewer_outcome == ReviewOutcome.ACCEPTED,
        )
    )
    if already:
        return False
    _attach_to_claim(session, source, claim)
    source.reviewer_outcome = ReviewOutcome.ACCEPTED
    source.auto_accepted = True
    source.credibility = credibility
    source.reviewed_at = datetime.now(UTC)
    audit.record(
        session,
        None,
        "external_source.auto_accepted",
        "external_source",
        source.id,
        claim_id=str(source.claim_id) if source.claim_id else None,
        criterion_code=source.criterion_code,
        url=source.url,
        source_type=source.source_type,
        credibility=credibility.value,
    )
    return True


def auto_accept_pending_sources(session: Session) -> int:
    """Apply automatic acceptance to findings from earlier research still awaiting review.
    Returns how many were accepted; the caller commits."""
    pending = session.scalars(
        select(ExternalSource)
        .where(
            ExternalSource.reviewer_outcome == ReviewOutcome.PENDING,
            ExternalSource.superseded_at.is_(None),
        )
        .order_by(ExternalSource.created_at, ExternalSource.id)
    ).all()
    return sum(auto_accept_external_source(session, source) for source in pending)


def review_external_source(
    session: Session,
    source: ExternalSource,
    reviewer: User,
    outcome: ReviewOutcome,
    *,
    credibility: QualityLevel | None = None,
    identity_confirmed: bool = False,
    notes: str | None = None,
) -> None:
    """Accept a public-web finding as evidence for its claim or criterion, or reject it
    (spec §6). A finding accepted automatically can still be rejected, with notes."""
    notes = (notes or "").strip() or None
    overturning = (
        source.auto_accepted
        and source.reviewer_outcome == ReviewOutcome.ACCEPTED
        and source.reviewed_by is None
        and outcome == ReviewOutcome.REJECTED
    )
    if source.reviewer_outcome != ReviewOutcome.PENDING and not overturning:
        raise SourceReviewRejected(
            f"this finding was already {source.reviewer_outcome.value}", conflict=True
        )
    if source.superseded_at is not None:
        raise SourceReviewRejected("a later research run replaced this finding", conflict=True)
    if outcome == ReviewOutcome.PENDING:
        raise SourceReviewRejected("choose accepted or rejected")
    if outcome == ReviewOutcome.REJECTED and not notes:
        raise SourceReviewRejected("notes are required to reject a finding")
    claim = session.get(Claim, source.claim_id) if source.claim_id else None
    if outcome == ReviewOutcome.ACCEPTED:
        if source.claim_id and (claim is None or claim.superseded_at is not None):
            raise SourceReviewRejected(
                "the claim this finding concerns was replaced by a later extraction; "
                "research again",
                conflict=True,
            )
        if credibility is None:
            raise SourceReviewRejected("rate the source's credibility before accepting it")
        if source.identity_confidence != QualityLevel.HIGH and not identity_confirmed:
            raise SourceReviewRejected(
                "the page may be about a namesake: confirm it concerns this nominee"
            )
        _attach_to_claim(session, source, claim)
    if overturning:
        session.execute(delete(ClaimEvidence).where(ClaimEvidence.external_source_id == source.id))
    source.reviewer_outcome = outcome
    source.credibility = credibility
    source.identity_confirmed = identity_confirmed or source.identity_confirmed
    source.review_notes = notes
    source.reviewed_by = reviewer.id
    source.reviewed_at = datetime.now(UTC)
    audit.record(
        session,
        reviewer,
        f"external_source.{outcome.value}",
        "external_source",
        source.id,
        claim_id=str(source.claim_id) if source.claim_id else None,
        criterion_code=source.criterion_code,
        url=source.url,
        credibility=credibility.value if credibility else None,
        notes=notes,
        overturned_auto_acceptance=overturning,
    )
