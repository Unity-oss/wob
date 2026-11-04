"""Estimated AI spend per pipeline step, from the token and tool usage each job records.

An estimate at Anthropic's published list prices (USD), not a bill: OpenRouter, negotiated
rates and US-only inference change what is actually charged. Embeddings and reranking run on the
server and cost nothing per call.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

PRICES_AS_OF = "2026-10-23"
# USD per million tokens: base input, 5-minute cache write, cache read, output.
# https://platform.claude.com/docs/en/about-claude/pricing
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 12.5, 0.25, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 0.10, 10.0),
    # Prompts up to 100,000 tokens; longer ones cost five times as much.
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
}
WEB_SEARCH_PER_SEARCH = 10.0 / 1000  # web fetch has no fee beyond its tokens


def price_key(model: str | None) -> str | None:
    """The price-table entry for a recorded model name, e.g. 'anthropic/claude-opus-5.5' or
    'claude-opus-5-5-20261001'."""
    if not model:
        return None
    name = model.split(":", 1)[-1].split("/", 1)[-1].replace(".", "-").lower()
    return next((key for key in PRICES if name.startswith(key)), None)


@dataclass
class Step:
    key: str
    label: str
    detail: str
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    def add_usage(self, model: str | None, usage: dict[str, Any]) -> None:
        tokens = (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("cache_creation_input_tokens", 0) or 0),
            int(usage.get("cache_read_input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )
        self.input_tokens += tokens[0]
        self.cache_write_tokens += tokens[1]
        self.cache_read_tokens += tokens[2]
        self.output_tokens += tokens[3]
        key = price_key(model)
        if key is None:
            if any(tokens):
                self.unpriced_models.add(model or "unknown")
            return
        self.cost_usd += sum(t * p for t, p in zip(tokens, PRICES[key], strict=True)) / 1e6


_FILTER = """
    WHERE r.created_at >= :since
      AND (CAST(:category AS uuid) IS NULL OR {category_column} = CAST(:category AS uuid))
"""
_CLAIMS = text(
    "SELECT coalesce(r.served_by_models[1], r.llm_model) AS model, r.usage "
    "FROM claim_extraction_runs r "
    "JOIN finalist_categories fc ON fc.id = r.finalist_category_id"
    + _FILTER.format(category_column="fc.category_id")
)
_RESEARCH = text(
    "SELECT coalesce(r.served_by_models[1], r.llm_model) AS model, r.usage, r.searches, "
    "r.pages_fetched FROM research_runs r "
    "JOIN finalist_categories fc ON fc.id = r.finalist_category_id"
    + _FILTER.format(category_column="fc.category_id")
)
_ASSESSMENT = text(
    "SELECT coalesce(r.served_by_models[1], r.llm_model) AS model, r.usage, "
    "(SELECT count(*) FROM criterion_assessments a WHERE a.run_id = r.id AND a.source = 'ai') "
    "AS ratings FROM assessment_runs r" + _FILTER.format(category_column="r.category_id")
)


def _plural(n: int, word: str) -> str:
    plural = word + ("es" if word.endswith(("s", "sh", "ch", "x")) else "s")
    return f"{n:,} {word if n == 1 else plural}"


def ai_costs(session: Session, days: int | None, category_id: uuid.UUID | None) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days) if days else datetime(2000, 1, 1, tzinfo=UTC)
    params = {"since": since, "category": str(category_id) if category_id else None}

    claims = session.execute(_CLAIMS, params).all()
    extraction = Step("claims", "Claim extraction", _plural(len(claims), "run"))
    for model, usage in claims:
        extraction.add_usage(model, usage or {})

    research = session.execute(_RESEARCH, params).all()
    searches = sum(row.searches or 0 for row in research)
    pages = sum(row.pages_fetched or 0 for row in research)
    reading = Step(
        "research",
        "Research · Claude reading and reasoning",
        f"{_plural(len(research), 'run')}, {_plural(pages, 'page')} read",
    )
    for row in research:
        reading.add_usage(row.model, row.usage or {})
    web_search = Step("web_search", "Research · web search", _plural(searches, "search"))
    web_search.cost_usd = searches * WEB_SEARCH_PER_SEARCH
    web_fetch = Step(
        "web_fetch", "Research · web fetch", f"{_plural(pages, 'page')}, no fee beyond tokens"
    )

    runs = session.execute(_ASSESSMENT, params).all()
    ratings = sum(row.ratings for row in runs)
    assessment = Step(
        "assessment",
        "Assessment",
        f"{_plural(len(runs), 'run')}, {_plural(ratings, 'criterion rating')}",
    )
    for row in runs:
        assessment.add_usage(row.model, row.usage or {})

    retrieval = Step(
        "retrieval", "Evidence search and reranking", "runs on the server, no per-call cost"
    )
    steps = [extraction, reading, web_search, web_fetch, assessment, retrieval]

    notes = [
        f"Estimated at Anthropic's list prices of {PRICES_AS_OF}. Requests through OpenRouter, "
        "negotiated rates and US-only inference are billed differently.",
        "Linking research findings to claims isn't recorded yet: about one short Claude call "
        "per candidate after each research run.",
    ]
    unpriced = sorted({m for s in steps for m in s.unpriced_models})
    if unpriced:
        notes.append(f"No price is known for {', '.join(unpriced)}; its tokens count as $0.")
    if any(price_key(row.model) == "claude-haiku-5-5" for row in research):
        notes.append(
            "Research on Claude Haiku 5.5 is priced at its rate for prompts up to 100,000 tokens; "
            "longer requests cost five times as much, so research may cost more than shown."
        )
    if any("cache_read_input_tokens" not in (row.usage or {}) for row in research):
        notes.append(
            "Research runs before 23 October 2026 didn't record cached tokens, so their cost is "
            "slightly understated."
        )

    return {
        "days": days,
        "category_id": category_id,
        "prices_as_of": PRICES_AS_OF,
        "total_usd": sum(s.cost_usd for s in steps),
        "steps": [
            {
                "key": s.key,
                "label": s.label,
                "detail": s.detail,
                "input_tokens": s.input_tokens,
                "cache_write_tokens": s.cache_write_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "output_tokens": s.output_tokens,
                "cost_usd": s.cost_usd,
            }
            for s in steps
        ],
        "prices": [
            {
                "model": model,
                "input": p[0],
                "cache_write": p[1],
                "cache_read": p[2],
                "output": p[3],
            }
            for model, p in PRICES.items()
        ],
        "web_search_per_1000": WEB_SEARCH_PER_SEARCH * 1000,
        "notes": notes,
    }
