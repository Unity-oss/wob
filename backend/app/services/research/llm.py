"""Claude with server-side web search and fetch, for external research (FR-13)."""

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.config import Settings
from app.services.claims.llm import (
    AIProcessingNotApproved,
    ExtractionLLMError,
    ExtractionRefused,
    OutputTruncated,
    _retryable,
    get_claim_llm,
    hosted_client,
    request_options,
)
from app.services.research.schema import ResearchOutput
from app.services.tracing import tag_model, trace_llm, usage_metadata


@dataclass
class FetchedPage:
    url: str
    text: str
    retrieved_at: str | None
    title: str | None = None


@dataclass
class ResearchResult:
    output: ResearchOutput
    pages: list[FetchedPage]
    served_by: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    searches: int = 0


class ResearchLLM(Protocol):
    model_id: str

    def research(self, system: str, user: str) -> ResearchResult: ...


def _pdf_text(data: bytes) -> str:
    from app.services.extraction import Ocr, extract
    from app.services.file_types import PDF

    result = extract(data, PDF.mime_type, Ocr())
    return "\n\n".join(block.render() for block in result.blocks)


def page_from_fetch_block(block: Any) -> FetchedPage | None:
    """Text of a web_fetch_tool_result, or None for a fetch error."""
    content = block.content
    if getattr(content, "type", None) != "web_fetch_result":
        return None
    document = content.content
    source = document.source
    if source.type == "text":
        text = source.data
    elif source.type == "base64" and source.media_type == "application/pdf":
        text = _pdf_text(base64.b64decode(source.data))
    else:
        return None
    return FetchedPage(content.url, text, content.retrieved_at, getattr(document, "title", None))


class AnthropicResearchLLM:
    """The basic web_search_20250305 / web_fetch_20250910 tool versions are used on purpose:
    the dynamic-filtering versions filter fetched pages in code before Claude reads them, so
    the raw page text needed to verify each quote would not come back in the response."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        settings = research_settings(settings)
        self.model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._settings = settings
        self._client = client or hosted_client(settings)
        self._options = request_options(settings)
        if settings.llm_provider == "anthropic":
            if not supports_server_fallback(settings.llm_model):
                self._options = {}
            # Cache the growing prefix, so pages already fetched cost cache-read rates when a
            # paused turn is resumed.
            self._options = {**self._options, "cache_control": {"type": "ephemeral"}}
        self._format = {"type": "json_schema", "schema": anthropic.transform_schema(ResearchOutput)}

    def _tools(self) -> list[dict[str, Any]]:
        s = self._settings
        search: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": s.research_max_searches,
            "blocked_domains": s.research_blocked_domains,
        }
        if s.research_country:
            search["user_location"] = {"type": "approximate", "country": s.research_country}
        return [
            search,
            {
                "type": "web_fetch_20250910",
                "name": "web_fetch",
                "max_uses": s.research_max_fetches,
                "blocked_domains": s.research_blocked_domains,
                "max_content_tokens": s.research_max_fetch_tokens,
            },
        ]

    @trace_llm(
        "claude research",
        lambda result: {
            "usage_metadata": usage_metadata(result.usage),
            "searches": result.searches,
            "pages_fetched": len(result.pages),
        },
    )
    def research(self, system: str, user: str) -> ResearchResult:
        s = self._settings
        tag_model(self.model_id.split(":", 1)[0], s.llm_model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        pages: list[FetchedPage] = []
        served_by: list[str] = []
        usage: dict[str, int] = {}
        searches = 0
        for _ in range(s.research_max_continuations):
            try:
                with self._client.beta.messages.stream(
                    model=s.llm_model,
                    max_tokens=s.claim_max_output_tokens,
                    system=system,
                    messages=messages,
                    tools=self._tools(),
                    thinking={"type": "adaptive"},
                    output_config={"effort": s.research_effort, "format": self._format},
                    **self._options,
                ) as stream:
                    message = stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
                raise _retryable(exc) from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    raise _retryable(exc) from exc
                raise ExtractionLLMError(
                    f"Claude API error {exc.status_code}: {exc.message}"
                ) from exc

            if message.model not in served_by:
                served_by.append(message.model)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                usage[key] = usage.get(key, 0) + (getattr(message.usage, key, 0) or 0)
            for block in message.content:
                if block.type == "web_fetch_tool_result":
                    page = page_from_fetch_block(block)
                    if page:
                        pages.append(page)
                elif block.type == "server_tool_use" and block.name == "web_search":
                    searches += 1

            if message.stop_reason == "pause_turn":
                # Resume the server-side tool loop: resend the paused turn, no new user text.
                messages.append({"role": "assistant", "content": message.content})
                continue
            if message.stop_reason == "refusal":
                category = getattr(message.stop_details, "category", None)
                raise ExtractionRefused(f"the model declined the request (category: {category})")
            if message.stop_reason == "max_tokens":
                raise OutputTruncated("the research output exceeded max_tokens")
            texts = [b.text for b in message.content if b.type == "text"]
            if not texts:
                raise ExtractionLLMError(f"no output (stop_reason={message.stop_reason})")
            output = ResearchOutput.model_validate_json(texts[-1])
            return ResearchResult(output, pages, served_by, usage, searches)
        raise ExtractionLLMError(
            f"research did not finish within {s.research_max_continuations} continuations"
        )


class ScriptedResearchLLM:
    """For tests: returns a fixed output together with the pages it 'fetched'."""

    model_id = "fake:scripted"

    def __init__(self, respond: Callable[[str, str], ResearchResult | Exception]) -> None:
        self._respond = respond
        self.calls: list[tuple[str, str]] = []

    def research(self, system: str, user: str) -> ResearchResult:
        self.calls.append((system, user))
        result = self._respond(system, user)
        if isinstance(result, Exception):
            raise result
        return result


def supports_server_fallback(model: str) -> bool:
    """Claude Haiku models have no server-side refusal fallback; sending one is rejected."""
    return not model.startswith("claude-haiku")


def research_settings(settings: Settings) -> Settings:
    """The settings research runs with: LLM_PROVIDER unless RESEARCH_PROVIDER overrides it,
    and RESEARCH_MODEL on the Claude API."""
    if settings.llm_provider not in ("anthropic", "openrouter"):
        return settings
    return settings.model_copy(
        update={
            "llm_provider": settings.research_provider or settings.llm_provider,
            "claim_model": settings.research_model,
        }
    )


def get_research_llm(settings: Settings) -> ResearchLLM:
    if not settings.research_enabled:
        raise AIProcessingNotApproved("external research is disabled (RESEARCH_ENABLED=false)")
    get_claim_llm(settings)  # same provider and approval checks as claim extraction
    settings = research_settings(settings)
    if settings.llm_provider not in ("anthropic", "openrouter"):
        raise AIProcessingNotApproved("external research needs a hosted LLM provider")
    key = settings.anthropic_api_key
    if settings.research_provider == "anthropic" and not (key and key.get_secret_value()):
        raise AIProcessingNotApproved("RESEARCH_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
    if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
        raise AIProcessingNotApproved("research through OpenRouter needs OPENROUTER_API_KEY")
    return AnthropicResearchLLM(settings)


def research_status(settings: Settings) -> dict[str, Any]:
    if settings.llm_provider == "fake" and settings.research_enabled:
        return {"enabled": settings.environment != "production", "reason": None}
    try:
        get_research_llm(settings)
    except AIProcessingNotApproved as exc:
        return {"enabled": False, "reason": str(exc)}
    return {"enabled": True, "reason": None, "model": research_settings(settings).llm_model}
