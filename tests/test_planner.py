"""Tests for planner completion configuration."""

from __future__ import annotations

import sys
from typing import Any

import pytest

# planner.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.planner import (  # noqa: E402
    _PLAN_REPAIR_PROMPT,
    _create_planner_completion,
    _run_planner_iteration,
)
from dgov.workers.config import AtomicConfig  # noqa: E402
from dgov.workers.provider import InvalidPlanOutputError  # noqa: E402

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


class _InvalidPlanProvider:
    """Provider that always raises InvalidPlanOutputError."""

    def create_chat_completion(self, **_kwargs: Any) -> object:
        raise InvalidPlanOutputError(
            "Claude Code provider did not return valid emit_plan JSON.",
            excerpt="garbled output here",
        )


def test_planner_appends_repair_prompt_on_first_invalid_plan_json() -> None:
    """First invalid emit_plan output appends a repair prompt and signals retry."""
    messages: list[Any] = [{"role": "user", "content": "plan task"}]

    done, nudged = _run_planner_iteration(
        _InvalidPlanProvider(),
        "model",
        messages,
        AtomicConfig(),
        False,
        None,
        frozenset({"emit_plan"}),
        None,
        0,
        False,
        lambda: None,
    )

    assert not done
    assert not nudged
    assert len(messages) == 2
    repair = messages[-1]
    assert repair["role"] == "user"
    assert "JSON" in repair["content"]
    assert "garbled output here" in repair["content"]


def test_planner_exits_on_repeated_invalid_plan_json(capsys: pytest.CaptureFixture) -> None:
    """Second invalid emit_plan output emits a clear error and exits nonzero."""
    messages: list[Any] = [
        {"role": "user", "content": "plan task"},
        {"role": "user", "content": f"{_PLAN_REPAIR_PROMPT}\n\nDiagnostic excerpt: prior"},
    ]

    with pytest.raises(SystemExit) as exc_info:
        _run_planner_iteration(
            _InvalidPlanProvider(),
            "model",
            messages,
            AtomicConfig(),
            False,
            None,
            frozenset({"emit_plan"}),
            None,
            0,
            False,
            lambda: None,
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid emit_plan output after repair attempt" in captured.out
