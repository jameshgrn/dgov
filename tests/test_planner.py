"""Tests for planner completion configuration."""

from __future__ import annotations

import sys
from typing import Any

import pytest

# planner.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.planner import _create_planner_completion  # noqa: E402
from dgov.workers.config import AtomicConfig  # noqa: E402

pytestmark = pytest.mark.unit


class _CaptureProvider:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create_chat_completion(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        return object()


def test_planner_completion_uses_configured_provider_max_tokens() -> None:
    provider = _CaptureProvider()

    _create_planner_completion(
        provider,
        model="provider/model",
        messages=[{"role": "user", "content": "hi"}],
        interactive=False,
        config=AtomicConfig(llm_max_tokens=8192),
    )

    assert provider.kwargs["max_tokens"] == 8192
