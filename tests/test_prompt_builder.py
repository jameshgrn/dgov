"""Tests for worker prompt construction helpers."""

from __future__ import annotations

import pytest

from dgov.dag_parser import DagTaskSpec
from dgov.prompt_builder import PromptBuilder

pytestmark = pytest.mark.unit


def test_settlement_retry_prompt_requires_rerunning_failed_verification() -> None:
    task = DagTaskSpec(
        slug="fix-tests",
        summary="Fix tests",
        prompt="Update the implementation.",
    )

    prompt = PromptBuilder.settlement_retry_prompt(
        task,
        "Test failure from `uv run pytest tests/test_x.py -q`:\nFAILED test_x.py::test_x",
    )

    assert "uv run pytest tests/test_x.py -q" in prompt
    assert "rerun the failing verification command" in prompt
    assert "before calling done" in prompt
    assert "SETTLEMENT VERDICT AND EVIDENCE" in prompt


def test_settlement_retry_prompt_preserves_evidence_narrative() -> None:
    task = DagTaskSpec(
        slug="fix-integration",
        summary="Fix integration",
        prompt="Update the implementation.",
    )

    prompt = PromptBuilder.settlement_retry_prompt(
        task,
        "Semantic gate 'same_symbol_edit' rejected\n\n"
        "Settlement evidence:\n"
        "same-symbol edit: function foo in src/a.py",
    )

    assert "same-symbol edit: function foo in src/a.py" in prompt
