"""Retry helpers for OpenAI-compatible LLM calls."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

RATE_LIMIT_BACKOFF_S = (5.0, 30.0, 90.0)
_JITTER_FRACTION = 0.2


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


def _jittered_delay(
    base_delay_s: float,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    jitter_span = base_delay_s * _JITTER_FRACTION
    return max(0.0, base_delay_s + jitter_fn(-jitter_span, jitter_span))


def call_with_rate_limit_backoff[T](
    fn: Callable[[], T],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
    backoff_s: tuple[float, ...] = RATE_LIMIT_BACKOFF_S,
) -> T:
    """Call ``fn`` with slow retries for provider 429/rate-limit failures."""
    for delay_s in (*backoff_s, None):
        try:
            return fn()
        except Exception as exc:
            if delay_s is None or not _is_rate_limit_error(exc):
                raise
            sleep_fn(_jittered_delay(delay_s, jitter_fn))
    raise RuntimeError("unreachable rate-limit retry state")


def create_chat_completion_with_backoff(client: Any, **kwargs: Any) -> Any:
    """Call ``client.chat.completions.create`` with dgov's rate-limit backoff."""
    return call_with_rate_limit_backoff(lambda: client.chat.completions.create(**kwargs))
