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
