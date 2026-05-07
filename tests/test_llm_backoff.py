"""Tests for LLM provider retry backoff."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dgov.llm_backoff import (
    FireworksRateLimitError,
    _estimate_request_tokens,
    _estimate_tokens_from_length,
    _extract_fireworks_limits,
    _extract_retry_after,
    _get_header,
    _get_headers_from_exc,
    _jittered_delay,
    call_with_rate_limit_backoff,
    create_chat_completion_with_backoff,
)

pytestmark = pytest.mark.unit


class _RateLimitError(Exception):
    status_code = 429


class _ResponseRateLimitError(Exception):
    def __init__(self) -> None:
        super().__init__("provider throttled")
        self.response = SimpleNamespace(status_code=429)


class _FireworksRateLimitError(Exception):
    """Simulates Fireworks 429 with TPM limit headers."""

    def __init__(
        self,
        prompt_limit: int | None = None,
        generated_limit: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__("rate limit exceeded")
        headers: dict[str, str] = {}
        if prompt_limit is not None:
            headers["X-Ratelimit-Limit-Tokens-Prompt"] = str(prompt_limit)
        if generated_limit is not None:
            headers["X-Ratelimit-Limit-Tokens-Generated"] = str(generated_limit)
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        # Fireworks returns headers on the response object
        self.response = SimpleNamespace(status_code=429, headers=headers)


class _DirectHeadersRateLimitError(Exception):
    """Simulates exception with headers directly on exception (not response)."""

    def __init__(self) -> None:
        super().__init__("rate limit")
        self.status_code = 429
        self.headers = {"retry-after": "42"}


def test_rate_limit_backoff_uses_slow_schedule_with_jitter_hook() -> None:
    calls = 0
    sleeps: list[float] = []

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _RateLimitError("rate limited")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 3
    assert sleeps == [5.0, 30.0]


def test_rate_limit_backoff_exhausts_after_90_second_slot() -> None:
    sleeps: list[float] = []

    with pytest.raises(_RateLimitError):
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(_RateLimitError("too many requests")),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
        )

    assert sleeps == [5.0, 30.0, 90.0]


def test_rate_limit_backoff_does_not_retry_non_rate_limit_errors() -> None:
    sleeps: list[float] = []

    with pytest.raises(RuntimeError):
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            sleep_fn=sleeps.append,
        )

    assert sleeps == []


def test_rate_limit_detection_accepts_response_status_code() -> None:
    calls = 0
    error = _ResponseRateLimitError()

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=lambda _delay: None,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 2


def test_jittered_delay_applies_bounded_twenty_percent_jitter() -> None:
    assert _jittered_delay(10.0, lambda _lo, hi: hi) == pytest.approx(12.0)
    assert _jittered_delay(10.0, lambda lo, _hi: lo) == pytest.approx(8.0)


# === New Fireworks-specific tests ===


def test_generic_429_still_retries_on_5_30_90_schedule() -> None:
    """Generic 429 without Fireworks headers uses the standard backoff schedule."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise _RateLimitError("rate limited")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 4
    assert sleeps == [5.0, 30.0, 90.0]


def test_retry_after_header_controls_first_sleep() -> None:
    """Retry-After header value is used instead of the first static backoff slot."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _FireworksRateLimitError(retry_after="15")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
        _kwargs_for_classification={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert result == "ok"
    assert calls == 2
    # First sleep uses Retry-After value (15) with jitter applied
    assert sleeps[0] == pytest.approx(15.0)


def test_fireworks_prompt_limit_below_estimated_request_size_fails_fast() -> None:
    """When prompt limit is below estimated tokens, fail immediately without retry."""
    sleeps: list[float] = []

    # Create a message that will estimate to more than 100 tokens
    # Using 400+ chars to exceed 100 tokens at 4 chars/token
    long_content = "x" * 400  # 100 tokens at 4 chars/token

    error = _FireworksRateLimitError(prompt_limit=50)

    with pytest.raises(FireworksRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={"messages": [{"role": "user", "content": long_content}]},
        )

    # Should fail immediately with no sleep
    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "prompt"
    assert exc_info.value.estimated_tokens >= 100
    assert exc_info.value.observed_limit == 50
    assert "Fireworks adaptive serverless TPM" in str(exc_info.value)
    assert "Suggested actions:" in str(exc_info.value)


def test_fireworks_generated_limit_below_max_tokens_fails_fast() -> None:
    """When generated limit is below max_tokens, fail immediately without retry."""
    sleeps: list[float] = []

    error = _FireworksRateLimitError(generated_limit=100)

    with pytest.raises(FireworksRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 500,  # Exceeds 100 limit
            },
        )

    # Should fail immediately with no sleep
    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "generated"
    assert exc_info.value.estimated_tokens == 500
    assert exc_info.value.observed_limit == 100
    assert "Fireworks adaptive serverless TPM" in str(exc_info.value)


def test_header_lookup_works_case_insensitively() -> None:
    """Headers are looked up case-insensitively from response.headers."""
    headers = {
        "X-Ratelimit-Limit-Tokens-Prompt": "1000",
        "x-ratelimit-limit-tokens-generated": "500",
        "RETRY-AFTER": "30",
        "Content-Type": "application/json",
    }

    assert _get_header(headers, "x-ratelimit-limit-tokens-prompt") == "1000"
    assert _get_header(headers, "X-RateLimit-Limit-Tokens-Prompt") == "1000"
    assert _get_header(headers, "x-ratelimit-limit-tokens-generated") == "500"
    assert _get_header(headers, "retry-after") == "30"
    assert _get_header(headers, "Retry-After") == "30"
    assert _get_header(headers, "nonexistent") is None


def test_extract_fireworks_limits_parses_headers() -> None:
    """_extract_fireworks_limits correctly parses Fireworks TPM limit headers."""
    headers = {
        "X-Ratelimit-Limit-Tokens-Prompt": "1000",
        "x-ratelimit-limit-tokens-generated": "500",
    }

    prompt_limit, generated_limit = _extract_fireworks_limits(headers)

    assert prompt_limit == 1000
    assert generated_limit == 500


def test_extract_fireworks_limits_returns_none_for_missing() -> None:
    """_extract_fireworks_limits returns None for missing headers."""
    headers: dict[str, str] = {}

    prompt_limit, generated_limit = _extract_fireworks_limits(headers)

    assert prompt_limit is None
    assert generated_limit is None


def test_extract_retry_after_parses_header() -> None:
    """_extract_retry_after correctly parses Retry-After header."""
    assert _extract_retry_after({"retry-after": "30"}) == 30.0
    assert _extract_retry_after({"Retry-After": "15.5"}) == 15.5
    assert _extract_retry_after({}) is None
    assert _extract_retry_after({"retry-after": "invalid"}) is None


def test_get_headers_from_exc_extracts_from_response() -> None:
    """_get_headers_from_exc extracts headers from exc.response.headers."""
    error = _FireworksRateLimitError(prompt_limit=100)
    headers = _get_headers_from_exc(error)

    assert "X-Ratelimit-Limit-Tokens-Prompt" in headers
    assert headers["X-Ratelimit-Limit-Tokens-Prompt"] == "100"


def test_get_headers_from_exc_extracts_from_exc_directly() -> None:
    """_get_headers_from_exc extracts headers from exc.headers when response is absent."""
    error = _DirectHeadersRateLimitError()
    headers = _get_headers_from_exc(error)

    assert headers.get("retry-after") == "42"


def test_estimate_tokens_from_length_rounds_up() -> None:
    """_estimate_tokens_from_length rounds up to nearest token."""
    assert _estimate_tokens_from_length("") == 0
    assert _estimate_tokens_from_length("a") == 1  # 1 char -> 1 token (rounded up)
    assert _estimate_tokens_from_length("abcd") == 1  # 4 chars -> 1 token
    assert _estimate_tokens_from_length("abcde") == 2  # 5 chars -> 2 tokens (rounded up)
    assert _estimate_tokens_from_length("x" * 100) == 25  # 100 chars -> 25 tokens
    assert _estimate_tokens_from_length("x" * 99) == 25  # 99 chars -> 25 tokens (rounded up)


def test_estimate_request_tokens_from_messages() -> None:
    """_estimate_request_tokens estimates tokens from message content."""
    kwargs = {
        "messages": [
            {"role": "system", "content": "x" * 40},  # 10 tokens
            {"role": "user", "content": "y" * 80},  # 20 tokens
        ]
    }

    prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    assert prompt_tokens == 30  # 10 + 20
    assert generated_tokens == 0  # No max_tokens


def test_estimate_request_tokens_with_tools() -> None:
    """_estimate_request_tokens includes tool schema in estimation."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "test",
                "description": "A test function",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    kwargs = {
        "messages": [{"role": "user", "content": "test"}],
        "tools": tools,
    }

    prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    # Should include both message content and tools JSON
    assert prompt_tokens > 10  # Tools JSON adds significant tokens
    assert generated_tokens == 0


def test_estimate_request_tokens_with_max_tokens() -> None:
    """_estimate_request_tokens extracts max_tokens as generated estimate."""
    kwargs = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    }

    _prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    assert generated_tokens == 256


def test_estimate_request_tokens_with_tool_calls_in_messages() -> None:
    """_estimate_request_tokens estimates tool_calls content in message history."""
    tool_call = {"id": "1", "type": "function", "function": {"name": "test", "arguments": "{}"}}
    kwargs = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call],
            }
        ]
    }

    prompt_tokens, _generated_tokens = _estimate_request_tokens(kwargs)

    assert prompt_tokens > 0  # Should count tool_calls JSON


def test_create_chat_completion_with_backoff_passes_kwargs_for_classification() -> None:
    """create_chat_completion_with_backoff passes kwargs to the backoff function."""
    # Create a mock client that fails with Fireworks limit error
    mock_client = MagicMock()
    error = _FireworksRateLimitError(prompt_limit=10)
    mock_client.chat.completions.create.side_effect = error

    with pytest.raises(FireworksRateLimitError):
        create_chat_completion_with_backoff(
            mock_client,
            messages=[{"role": "user", "content": "x" * 100}],  # Will estimate > 10 tokens
            max_tokens=100,
        )

    # Verify the client was called with the correct kwargs
    mock_client.chat.completions.create.assert_called_once_with(
        messages=[{"role": "user", "content": "x" * 100}],
        max_tokens=100,
    )


def test_fireworks_limit_exceeded_does_not_retry_other_limits() -> None:
    """When prompt limit exceeded but generated limit OK, only prompt error is raised."""
    sleeps: list[float] = []

    error = _FireworksRateLimitError(prompt_limit=10, generated_limit=1000)

    with pytest.raises(FireworksRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={
                "messages": [{"role": "user", "content": "x" * 100}],  # ~25 tokens
                "max_tokens": 50,  # Under 1000 limit
            },
        )

    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "prompt"


def test_fireworks_within_limits_allows_retry() -> None:
    """When request is within Fireworks limits, normal retry behavior occurs."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Rate limit error but within TPM limits (so should retry)
            raise _FireworksRateLimitError(prompt_limit=1000, generated_limit=1000)
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
        _kwargs_for_classification={
            "messages": [{"role": "user", "content": "hi"}],  # Small prompt
            "max_tokens": 50,  # Small generation request
        },
    )

    assert result == "ok"
    assert calls == 2
    assert len(sleeps) == 1  # Retried once
