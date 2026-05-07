"""Retry helpers for OpenAI-compatible LLM calls."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

RATE_LIMIT_BACKOFF_S = (5.0, 30.0, 90.0)
_JITTER_FRACTION = 0.2


class FireworksRateLimitError(Exception):
    """Raised when a request exceeds Fireworks adaptive serverless TPM limits.

    Attributes:
        limit_type: Either "prompt" or "generated" indicating which limit was exceeded.
        estimated_tokens: The estimated token count that exceeded the limit.
        observed_limit: The TPM limit observed from response headers.
    """

    def __init__(
        self,
        *,
        limit_type: str,
        estimated_tokens: int,
        observed_limit: int,
        message: str | None = None,
    ) -> None:
        self.limit_type = limit_type
        self.estimated_tokens = estimated_tokens
        self.observed_limit = observed_limit
        if message is None:
            message = self._build_message()
        super().__init__(message)

    def _build_message(self) -> str:
        actions = (
            "Suggested actions: shrink prompt/tool schema, reduce concurrency, "
            "use 'dgov run --continue' after cooldown, switch model/provider, "
            "or use an on-demand deployment."
        )
        return (
            f"Fireworks adaptive serverless TPM: {self.limit_type} token limit exceeded. "
            f"Estimated {self.limit_type} tokens: {self.estimated_tokens}, "
            f"observed limit: {self.observed_limit}. {actions}"
        )


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    if _status_code(exc) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _get_headers_from_exc(exc: Exception) -> Mapping[str, str]:
    """Extract headers from exception response, case-insensitively."""
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
    # Some clients put headers directly on the exception
    headers = getattr(exc, "headers", None)
    if headers is not None:
        return headers
    return {}


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Get header value case-insensitively."""
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def _extract_retry_after(headers: Mapping[str, str]) -> float | None:
    """Extract Retry-After header value in seconds."""
    value = _get_header(headers, "retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _extract_fireworks_limits(
    headers: Mapping[str, str],
) -> tuple[int | None, int | None]:
    """Extract Fireworks TPM limits from headers.

    Returns:
        Tuple of (prompt_limit, generated_limit) or (None, None) if not present.
    """
    import contextlib

    prompt_limit = _get_header(headers, "x-ratelimit-limit-tokens-prompt")
    generated_limit = _get_header(headers, "x-ratelimit-limit-tokens-generated")

    prompt_val: int | None = None
    generated_val: int | None = None

    if prompt_limit is not None:
        with contextlib.suppress(ValueError, TypeError):
            prompt_val = int(prompt_limit)

    if generated_limit is not None:
        with contextlib.suppress(ValueError, TypeError):
            generated_val = int(generated_limit)

    return (prompt_val, generated_val)


def _estimate_tokens_from_length(text: str) -> int:
    """Estimate tokens from text length using 4 chars per token heuristic."""
    if not text:
        return 0
    return (len(text) + 3) // 4  # Round up division


def _estimate_request_tokens(kwargs: dict[str, Any]) -> tuple[int, int]:
    """Estimate prompt and requested generated tokens from chat completion kwargs.

    Returns:
        Tuple of (estimated_prompt_tokens, estimated_generated_tokens).
        estimated_generated_tokens will be 0 if max_tokens is not specified.
    """
    estimated_prompt = 0

    # Estimate from messages
    messages = kwargs.get("messages", [])
    if messages:
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if content:
                    estimated_prompt += _estimate_tokens_from_length(str(content))
                # Function/tool calls in message history
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    estimated_prompt += _estimate_tokens_from_length(json.dumps(tool_calls))

    # Estimate from tools schema
    tools = kwargs.get("tools", [])
    if tools:
        tools_json = json.dumps(tools)
        estimated_prompt += _estimate_tokens_from_length(tools_json)

    # Estimate requested generated tokens
    estimated_generated = 0
    max_tokens = kwargs.get("max_tokens")
    if max_tokens is not None:
        try:
            estimated_generated = int(max_tokens)
        except (ValueError, TypeError):
            estimated_generated = 0

    return (estimated_prompt, estimated_generated)


def _check_fireworks_limits(
    exc: Exception,
    kwargs: dict[str, Any],
) -> None:
    """Check Fireworks TPM limits and raise FireworksRateLimitError if exceeded.

    This function extracts limits from response headers and compares against
    estimated request tokens. It raises immediately if limits are exceeded
    to avoid wasteful retries.
    """
    headers = _get_headers_from_exc(exc)
    prompt_limit, generated_limit = _extract_fireworks_limits(headers)

    if prompt_limit is None and generated_limit is None:
        return  # No Fireworks limits detected, let normal retry handle it

    estimated_prompt, estimated_generated = _estimate_request_tokens(kwargs)

    if prompt_limit is not None and estimated_prompt > prompt_limit:
        raise FireworksRateLimitError(
            limit_type="prompt",
            estimated_tokens=estimated_prompt,
            observed_limit=prompt_limit,
        )

    if generated_limit is not None and estimated_generated > generated_limit:
        raise FireworksRateLimitError(
            limit_type="generated",
            estimated_tokens=estimated_generated,
            observed_limit=generated_limit,
        )


def _jittered_delay(
    base_delay_s: float,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    jitter_span = base_delay_s * _JITTER_FRACTION
    return max(0.0, base_delay_s + jitter_fn(-jitter_span, jitter_span))


def _classify_rate_limit_error(
    exc: Exception,
    kwargs: dict[str, Any],
) -> tuple[bool, float | None]:
    """Classify a rate limit error and determine retry strategy.

    Returns:
        Tuple of (should_retry, delay_seconds).
        - should_retry: True if the error should be retried
        - delay_seconds: Custom delay to use (from Retry-After), or None to use default backoff
    """
    if not _is_rate_limit_error(exc):
        return (False, None)

    # Check Fireworks-specific limits first - fail fast if exceeded
    headers = _get_headers_from_exc(exc)
    prompt_limit, generated_limit = _extract_fireworks_limits(headers)

    if prompt_limit is not None or generated_limit is not None:
        # Fireworks headers present - validate limits
        estimated_prompt, estimated_generated = _estimate_request_tokens(kwargs)

        if prompt_limit is not None and estimated_prompt > prompt_limit:
            return (False, None)  # Don't retry, will raise FireworksRateLimitError

        if generated_limit is not None and estimated_generated > generated_limit:
            return (False, None)  # Don't retry, will raise FireworksRateLimitError

    # Check for Retry-After header (preferred over static backoff)
    retry_after = _extract_retry_after(headers)
    if retry_after is not None and retry_after > 0:
        return (True, retry_after)

    # Generic rate limit - use default backoff
    return (True, None)


def call_with_rate_limit_backoff[T](
    fn: Callable[[], T],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
    backoff_s: tuple[float, ...] = RATE_LIMIT_BACKOFF_S,
    _kwargs_for_classification: dict[str, Any] | None = None,
) -> T:
    """Call ``fn`` with slow retries for provider 429/rate-limit failures."""
    backoff_iter = iter(backoff_s)
    delay_s: float | None = None

    while True:
        try:
            return fn()
        except Exception as exc:
            should_retry, custom_delay = _classify_rate_limit_error(
                exc, _kwargs_for_classification or {}
            )

            if not should_retry:
                # Check if it's a Fireworks limit exceeded that we should raise specially
                if _kwargs_for_classification is not None:
                    _check_fireworks_limits(exc, _kwargs_for_classification)
                raise

            # Determine delay for this attempt
            if custom_delay is not None:
                delay_s = custom_delay
            else:
                try:
                    delay_s = next(backoff_iter)
                except StopIteration:
                    # Exhausted backoff slots, re-raise the original exception
                    raise exc from None

            sleep_fn(_jittered_delay(delay_s, jitter_fn))


def create_chat_completion_with_backoff(client: Any, **kwargs: Any) -> Any:
    """Call ``client.chat.completions.create`` with dgov's rate-limit backoff."""
    return call_with_rate_limit_backoff(
        lambda: client.chat.completions.create(**kwargs),
        _kwargs_for_classification=kwargs,
    )
