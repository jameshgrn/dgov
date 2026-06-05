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


def test_settlement_retry_prompt_omits_preexisting_sentrux_offenders() -> None:
    task = DagTaskSpec(
        slug="fix-architecture",
        summary="Fix architecture",
        prompt="Split the new helper.",
    )

    prompt = PromptBuilder.settlement_retry_prompt(
        task,
        "Sentrux architectural degradation:\n"
        "NEW Sentrux offenders:\n"
        "- Complex functions:\n"
        "  src/dgov/workers/atomic.py:531 AtomicTools._reject_network_egress\n"
        "PRE-EXISTING Sentrux offenders:\n"
        "- Complex functions:\n"
        "  src/dgov/sentrux_gate.py:1 stale_branch_noise\n",
    )

    assert "AtomicTools._reject_network_egress" in prompt
    assert "fix only NEW Sentrux offenders" in prompt
    assert "stale_branch_noise" not in prompt
    assert "src/dgov/sentrux_gate.py" not in prompt


def test_settlement_retry_prompt_omits_branch_verification_tail() -> None:
    task = DagTaskSpec(
        slug="fix-scope",
        summary="Fix scope",
        prompt="Update the claimed file.",
    )

    prompt = PromptBuilder.settlement_retry_prompt(
        task,
        "review:scope_violation - unclaimed.py changed\n"
        "branch verification: Type check failure:\n"
        "src/dgov/settlement.py:1654 stale branch diagnostic\n",
    )

    assert "unclaimed.py changed" in prompt
    assert "run-level evidence" in prompt
    assert "stale branch diagnostic" not in prompt
    assert "src/dgov/settlement.py:1654" not in prompt
