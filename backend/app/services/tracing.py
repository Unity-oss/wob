"""LangSmith tracing of AI jobs and Claude calls, metadata only.

Off unless LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set. Nominee data isn't covered for
LangSmith by the consent form or the data protection review, so traces never carry prompts,
evidence, AI output or error messages: inputs are reduced to internal IDs, outputs to token
usage and counts, and errors to their type. This is done here, in code, whatever LangSmith's
own settings say.
"""

import functools
from collections.abc import Callable, Mapping
from typing import Any

from langsmith import get_current_run_tree, traceable


class _Hidden(Exception):  # noqa: N818 - never seen outside this module
    """Stands in for an exception in the trace, so only its type is recorded."""

    def __init__(self, original: Exception) -> None:
        super().__init__(type(original).__name__)
        self.original = original


def _traced[F: Callable[..., Any]](fn: F, **options: Any) -> F:
    @functools.wraps(fn)
    def hide_errors(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise _Hidden(exc) from None

    traced = traceable(**options)(hide_errors)

    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return traced(*args, **kwargs)
        except _Hidden as hidden:
            original = hidden.original
            original.__suppress_context__ = True
            raise original from original.__cause__

    return call  # type: ignore[return-value]


def _ids(keys: tuple[str, ...]) -> Callable[[Mapping[str, Any]], dict[str, str]]:
    def keep(inputs: Mapping[str, Any]) -> dict[str, str]:
        return {k: str(inputs[k]) for k in keys if inputs.get(k) is not None}

    return keep


def _nothing(_: Any) -> dict[str, Any]:
    return {}


def trace_job[F: Callable[..., Any]](
    name: str, *id_args: str, run_type: str = "chain"
) -> Callable[[F], F]:
    """Trace a job or step; only the named ID arguments are recorded."""
    return lambda fn: _traced(
        fn, name=name, run_type=run_type, process_inputs=_ids(id_args), process_outputs=_nothing
    )


def usage_metadata(usage: Mapping[str, int]) -> dict[str, Any]:
    """Anthropic usage in LangSmith's form, so it can count tokens and cost."""
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    prompt = usage.get("input_tokens", 0) + cache_read + cache_creation
    completion = usage.get("output_tokens", 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "input_token_details": {"cache_read": cache_read, "cache_creation": cache_creation},
    }


def trace_llm[F: Callable[..., Any]](
    name: str, summarise: Callable[[Any], dict[str, Any]]
) -> Callable[[F], F]:
    """Trace a Claude call. `summarise` turns its result into counts, including usage_metadata;
    never text."""
    return lambda fn: _traced(
        fn, name=name, run_type="llm", process_inputs=_nothing, process_outputs=summarise
    )


def tag_model(provider: str, model: str) -> None:
    """Record the model on the current traced call, for LangSmith's cost tracking."""
    run = get_current_run_tree()
    if run is not None:
        run.add_metadata({"ls_provider": provider, "ls_model_name": model})
