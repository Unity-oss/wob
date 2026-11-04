"""LLM clients for claim extraction."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from app.config import Settings
from app.services.claims.schema import ClaimExtractionOutput
from app.services.tracing import tag_model, trace_llm, usage_metadata


class ExtractionLLMError(Exception):
    """Base class. `retryable` errors are worth another attempt later."""

    retryable = False


class OutputTruncated(ExtractionLLMError):
    """The response hit max_tokens; the batch should be split."""


class ExtractionRefused(ExtractionLLMError):
    pass


class AIProcessingNotApproved(ExtractionLLMError):
    pass


@dataclass
class LLMResult:
    output: ClaimExtractionOutput
    served_by: str
    usage: dict[str, int] = field(default_factory=dict)


class ClaimLLM(Protocol):
    model_id: str

    def extract(self, system: str, user: str) -> LLMResult: ...


BaseModelT = TypeVar("BaseModelT", bound=BaseModel)


# Server-side refusal fallback: a policy decline is retried on another model within the call.
SERVER_FALLBACK: dict[str, Any] = {
    "betas": ["server-side-fallback-2026-07-01"],
    "fallbacks": "default",
}


def request_options(settings: Settings) -> dict[str, Any]:
    """Provider-specific request options. OpenRouter rejects `fallbacks`; instead its routing is
    pinned to the configured providers, with no fallback to others and no data collection."""
    if settings.llm_provider == "openrouter":
        routing = {
            "only": settings.openrouter_providers,
            "allow_fallbacks": False,
            "data_collection": "deny",
        }
        return {"extra_body": {"provider": routing}}
    return SERVER_FALLBACK


def hosted_client(settings: Settings) -> anthropic.Anthropic:
    # The SDK retries 408/409/429/5xx and connection errors itself.
    if settings.llm_provider == "openrouter":
        key = settings.openrouter_api_key
        return anthropic.Anthropic(
            base_url=settings.openrouter_base_url,
            api_key=None,
            auth_token=key.get_secret_value() if key else None,
            max_retries=3,
        )
    key = settings.anthropic_api_key
    return anthropic.Anthropic(api_key=key.get_secret_value() if key else None, max_retries=3)


@lru_cache
def output_format(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": anthropic.transform_schema(model)}


class AnthropicClaimLLM:
    """Claude via the Messages API (directly, or through OpenRouter's Anthropic-compatible
    endpoint): structured JSON output, adaptive thinking, streaming (long outputs), and the
    provider's options (server-side refusal fallback on Anthropic; pinned routing on OpenRouter).
    """

    def __init__(
        self,
        model: str,
        effort: str,
        max_tokens: int,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
        *,
        provider: str = "anthropic",
        options: dict[str, Any] | None = None,
    ) -> None:
        self.model_id = f"{provider}:{model}"
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._options = SERVER_FALLBACK if options is None else options
        # The SDK retries 408/409/429/5xx and connection errors itself.
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    def extract(self, system: str, user: str) -> LLMResult:
        output, served_by, usage = self.call(system, user, ClaimExtractionOutput)
        return LLMResult(output=output, served_by=served_by, usage=usage)

    @trace_llm("claude", lambda result: {"usage_metadata": usage_metadata(result[2])})
    def call(
        self, system: str, user: str, output_model: type[BaseModelT]
    ) -> tuple[BaseModelT, str, dict[str, int]]:
        """One structured-output request; returns (parsed output, serving model, usage)."""
        tag_model(self.model_id.split(":", 1)[0], self._model)
        try:
            with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                # The schema goes in output_config rather than output_format: the SDK's
                # output_format parses while streaming, which raises on a refused or truncated
                # response before stop_reason can be checked.
                output_config={"effort": self._effort, "format": output_format(output_model)},
                **self._options,
            ) as stream:
                message = stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise _retryable(exc) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise _retryable(exc) from exc
            raise ExtractionLLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ExtractionRefused(f"the model declined the request (category: {category})")
        if message.stop_reason == "max_tokens":
            raise OutputTruncated("the output exceeded max_tokens")

        texts = [block.text for block in message.content if block.type == "text"]
        if not texts:
            raise ExtractionLLMError(f"no output (stop_reason={message.stop_reason})")
        usage = message.usage
        return (
            output_model.model_validate_json(texts[-1]),
            message.model,
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
        )


def _retryable(exc: Exception) -> ExtractionLLMError:
    error = ExtractionLLMError(f"Claude API unavailable: {exc}")
    error.retryable = True
    return error


class ScriptedClaimLLM:
    """Returns outputs produced by a function of the prompt. For tests only."""

    model_id = "fake:scripted"

    def __init__(self, respond: Callable[[str, str], ClaimExtractionOutput | Exception]) -> None:
        self._respond = respond
        self.calls: list[tuple[str, str]] = []

    def extract(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        result = self._respond(system, user)
        if isinstance(result, Exception):
            raise result
        return LLMResult(result, served_by="fake", usage={"input_tokens": len(user) // 4})


def get_claim_llm(settings: Settings) -> ClaimLLM:
    if settings.llm_provider == "disabled":
        raise AIProcessingNotApproved(
            "AI processing (claims, assessment, research) is disabled (LLM_PROVIDER=disabled)"
        )
    if settings.llm_provider in ("anthropic", "openrouter"):
        if not settings.external_ai_processing_approved:
            raise AIProcessingNotApproved(
                "sending nomination evidence to an external AI provider has not been approved "
                "(set EXTERNAL_AI_PROCESSING_APPROVED=true once the data protection review "
                "signs off; spec §12)"
            )
        if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
            raise AIProcessingNotApproved("LLM_PROVIDER=openrouter needs OPENROUTER_API_KEY")
        return AnthropicClaimLLM(
            settings.llm_model,
            settings.claim_effort,
            settings.claim_max_output_tokens,
            client=hosted_client(settings),
            provider=settings.llm_provider,
            options=request_options(settings),
        )
    if settings.environment == "production":
        raise AIProcessingNotApproved("the fake LLM is not allowed in production")
    raise AIProcessingNotApproved("LLM_PROVIDER=fake is only available inside tests")


def provider_status(settings: Settings) -> dict[str, Any]:
    """Whether claim extraction can run, and why not, for the UI."""
    try:
        if settings.llm_provider == "fake":
            return {"enabled": settings.environment != "production", "reason": None}
        get_claim_llm(settings)
    except AIProcessingNotApproved as exc:
        return {"enabled": False, "reason": str(exc)}
    return {"enabled": True, "reason": None, "model": settings.llm_model}
