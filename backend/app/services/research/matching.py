"""Links public sources accepted automatically for an award criterion to the candidate's claims
they support (FR-13), so they count as evidence on those claims and toward corroboration.

Claude proposes the links; one is kept only if its quote occurs in the source's text and both
references are known. Sources go to the AI only with the candidate's consent to AI processing.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Claim,
    ClaimEvidence,
    EvidenceRelation,
    ExternalSource,
    Finalist,
    FinalistCategory,
    ReviewOutcome,
)
from app.prompts.claim_matching import PROMPT_VERSION, SYSTEM_PROMPT, user_prompt
from app.services import audit, consent
from app.services.claims.llm import AnthropicClaimLLM
from app.services.research.research import passage_around
from app.services.tracing import trace_job

log = logging.getLogger("wob.research")


class ClaimMatch(BaseModel):
    source_ref: str = Field(description="The public source, e.g. S2")
    claim_ref: str = Field(description="A claim the source directly supports, e.g. C4")
    quote: str = Field(description="Text copied exactly from the source that shows the support")


class ClaimMatchOutput(BaseModel):
    matches: list[ClaimMatch]


class ClaimMatcher(Protocol):
    model_id: str

    def match(self, system: str, user: str) -> ClaimMatchOutput: ...


class AnthropicClaimMatcher:
    def __init__(self, llm: AnthropicClaimLLM) -> None:
        self.model_id = llm.model_id
        self._llm = llm

    def match(self, system: str, user: str) -> ClaimMatchOutput:
        return self._llm.call(system, user, ClaimMatchOutput)[0]


class ScriptedClaimMatcher:
    """For tests only."""

    model_id = "fake:scripted"

    def __init__(self, respond: Callable[[str, str], ClaimMatchOutput]) -> None:
        self._respond = respond
        self.calls: list[tuple[str, str]] = []

    def match(self, system: str, user: str) -> ClaimMatchOutput:
        self.calls.append((system, user))
        return self._respond(system, user)


@dataclass
class MatchOutcome:
    sources: int = 0
    links: int = 0
    discarded: int = 0


def match_criterion_sources(
    session: Session,
    matcher: ClaimMatcher,
    finalist_category_id: uuid.UUID,
    *,
    only_unmatched: bool = True,
) -> MatchOutcome:
    """Link this candidate's auto-accepted criterion sources to the current claims they support.
    With only_unmatched, sources already matched are skipped (new research); without it, all are
    matched again against the current claims (after re-extraction). The caller commits; errors
    from the AI propagate."""
    query = select(ExternalSource).where(
        ExternalSource.finalist_category_id == finalist_category_id,
        ExternalSource.auto_accepted,
        ExternalSource.reviewer_outcome == ReviewOutcome.ACCEPTED,
        ExternalSource.criterion_code.is_not(None),
    )
    if only_unmatched:
        query = query.where(ExternalSource.claims_matched_at.is_(None))
    sources = session.scalars(query.order_by(ExternalSource.created_at, ExternalSource.id)).all()
    claims = session.scalars(
        select(Claim)
        .where(Claim.finalist_category_id == finalist_category_id, Claim.superseded_at.is_(None))
        .order_by(Claim.created_at, Claim.id)
    ).all()
    if not sources or not claims:
        return MatchOutcome()
    if not consent.has(session, finalist_category_id, consent.Purpose.AI_PROCESSING):
        return MatchOutcome()

    fc = session.get_one(FinalistCategory, finalist_category_id)
    nominee = session.get_one(Finalist, fc.finalist_id).full_name
    source_refs = {f"S{i}": s for i, s in enumerate(sources, start=1)}
    claim_refs = {f"C{i}": c for i, c in enumerate(claims, start=1)}
    output = matcher.match(
        SYSTEM_PROMPT,
        user_prompt(
            nominee,
            [(ref, c.wording) for ref, c in claim_refs.items()],
            [(ref, s.publisher, s.evidence_passage) for ref, s in source_refs.items()],
        ),
    )

    linked = {
        (claim_id, source_id)
        for claim_id, source_id in session.execute(
            select(ClaimEvidence.claim_id, ClaimEvidence.external_source_id).where(
                ClaimEvidence.external_source_id.in_([s.id for s in sources])
            )
        )
    }
    outcome = MatchOutcome(sources=len(sources))
    now = datetime.now(UTC)
    for m in output.matches:
        source, claim = source_refs.get(m.source_ref), claim_refs.get(m.claim_ref)
        if (
            source is None
            or claim is None
            or passage_around(source.evidence_passage, m.quote) is None
        ):
            outcome.discarded += 1
            continue
        if (claim.id, source.id) in linked:
            continue
        linked.add((claim.id, source.id))
        session.add(
            ClaimEvidence(
                claim_id=claim.id,
                finalist_category_id=finalist_category_id,
                external_source_id=source.id,
                relation=EvidenceRelation.SUPPORTS,
                quoted_span=m.quote,
            )
        )
        outcome.links += 1
        audit.record(
            session,
            None,
            "external_source.matched_to_claim",
            "external_source",
            source.id,
            claim_id=str(claim.id),
            criterion_code=source.criterion_code,
            quote=m.quote,
            model=matcher.model_id,
            prompt_version=PROMPT_VERSION,
        )
    for source in sources:
        source.claims_matched_at = now
    return outcome


@trace_job("match sources to claims", "finalist_category_id")
def match_after_job(
    sessions: Callable[[], Session],
    matcher: ClaimMatcher | None,
    finalist_category_id: uuid.UUID,
    *,
    only_unmatched: bool,
) -> None:
    """Run matching after research or claim extraction has committed. A failure is logged and
    leaves sources unmatched for next time; it never fails the job that triggered it."""
    if matcher is None:
        return
    try:
        with sessions() as session:
            outcome = match_criterion_sources(
                session, matcher, finalist_category_id, only_unmatched=only_unmatched
            )
            session.commit()
    except Exception:
        log.exception("matching public sources to claims failed for %s", finalist_category_id)
        return
    if outcome.sources:
        log.info(
            "matched %d public source(s) for %s: %d link(s), %d discarded",
            outcome.sources,
            finalist_category_id,
            outcome.links,
            outcome.discarded,
        )
