"""Tests for LLM provider retry backoff."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dgov.llm_backoff import (
    _jittered_delay,
    call_with_rate_limit_backoff,
)

pytestmark = pytest.mark.unit


class _RateLimitError(Exception):
    status_code = 429


class _ResponseRateLimitError(Exception):
    def __init__(self) -> None:
        super().__init__("provider throttled")
        self.response = SimpleNamespace(status_code=429)


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
