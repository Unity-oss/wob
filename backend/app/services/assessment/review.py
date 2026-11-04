"""Human quality review of proposed criterion ratings (spec §7.1)."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import AssessmentStatus, CriterionAssessment, EvidenceRelation, User
from app.services import audit
from app.services.scales import Scale


class AssessmentReviewRejected(ValueError):
    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


def review_assessment(
    session: Session,
    assessment: CriterionAssessment,
    reviewer: User,
    decision: AssessmentStatus,
    *,
    adjusted_rating: int | None = None,
    notes: str | None = None,
    scale: Scale,
) -> None:
    notes = (notes or "").strip() or None
    if decision not in (AssessmentStatus.APPROVED, AssessmentStatus.REJECTED):
        raise AssessmentReviewRejected("decision must be approved or rejected")
    if decision == AssessmentStatus.REJECTED and not notes:
        raise AssessmentReviewRejected("explain why the rating is rejected")
    if adjusted_rating is not None:
        if not scale.contains(adjusted_rating):
            raise AssessmentReviewRejected(
                f"ratings are whole numbers from {scale.low} to {scale.high}"
            )
        if not notes:
            raise AssessmentReviewRejected("give the reason for adjusting the rating")
    if decision == AssessmentStatus.APPROVED:
        rating = assessment.rating if adjusted_rating is None else adjusted_rating
        supported = any(c.relation == EvidenceRelation.SUPPORTS for c in assessment.citations)
        if rating >= scale.cited_from and not supported:
            # Spec §13: no unsupported material claims in approved results.
            raise AssessmentReviewRejected(
                f"a rating of {scale.cited_from} or more needs at least one verified supporting "
                f"citation; reject it for re-assessment or adjust it to {scale.cited_from - 1}"
            )

    previous = {"status": assessment.status.value, "reviewer_rating": assessment.reviewer_rating}
    assessment.status = decision
    assessment.reviewer_rating = adjusted_rating
    assessment.review_notes = notes
    assessment.reviewed_by = reviewer.id
    assessment.reviewed_at = datetime.now(UTC)
    audit.record(
        session,
        reviewer,
        f"assessment.{decision.value}",
        "criterion_assessment",
        assessment.id,
        ai_rating=assessment.rating,
        adjusted_rating=adjusted_rating,
        notes=notes,
        previous=previous,
    )
