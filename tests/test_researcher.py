"""Tests for researcher.py prompt and config loading."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# researcher.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.researcher import _build_system_prompt  # noqa: E402
from dgov.workers.atomic import AtomicConfig  # noqa: E402

pytestmark = pytest.mark.unit


def test_researcher_prompt_uses_configured_budget_and_tree(tmp_path: Path) -> None:
    for idx in range(90):
        (tmp_path / f"file_{idx:03}.txt").write_text("x\n")

    config = AtomicConfig(
        worker_iteration_budget=75,
        worker_iteration_warn_at=60,
        worker_tree_max_lines=0,
    )
    prompt = _build_system_prompt(tmp_path, config)

    assert "75 tool calls" in prompt
    assert "past call" in prompt
    assert "60" in prompt
    assert "file_089.txt" in prompt


def test_researcher_prompt_defaults_to_read_first(tmp_path: Path) -> None:
    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "default mode is read-first analysis" in prompt
    assert "Do NOT modify code unless the goal explicitly asks" in prompt
