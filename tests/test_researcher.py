"""Tests for researcher.py prompt and config loading."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# researcher.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.researcher import (  # noqa: E402
    _build_system_prompt,
    run_researcher,
)
from dgov.worker import _execute_tool_call  # noqa: E402
from dgov.workers.atomic import (  # noqa: E402
    AtomicConfig,
    AtomicTools,
    get_allowed_tool_names,
    get_tool_spec,
)

pytestmark = pytest.mark.unit


def test_researcher_prompt_uses_configured_budget_and_repo_map(tmp_path: Path) -> None:
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


def test_researcher_prompt_uses_repo_map_language(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def hello():\n    return 1\n")

    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "REPO MAP:" in prompt
    assert "def hello" in prompt
    assert "ast_grep" in prompt


def test_researcher_prompt_defaults_to_read_first(tmp_path: Path) -> None:
    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "default mode is read-only analysis" in prompt
    assert "read-only by construction" in prompt
    assert "Editing tools are intentionally unavailable" in prompt


def test_researcher_prompt_requires_executive_summary(tmp_path: Path) -> None:
    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "governor-facing executive summary" in prompt
    assert "single short paragraph only" in prompt
    assert "<=120 words" in prompt
    assert "No headings, no bullets, no tables, no code blocks, no markdown" in prompt
    assert "follow-up" in prompt


def test_researcher_prompt_discourages_redundant_verify_loops(tmp_path: Path) -> None:
    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "Do NOT rerun the same command" in prompt
    assert "Re-run the same verify command repeatedly" in prompt


def test_researcher_prompt_requires_early_stop_once_answer_is_stable(tmp_path: Path) -> None:
    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "Stop as soon as the core question is answered" in prompt
    assert "2-3 decisive evidence points" in prompt
    assert "If the answer is already supported, do not spend remaining budget" in prompt
    assert "Keep gathering evidence after the answer is already stable" in prompt


def test_researcher_tool_spec_excludes_write_and_shell_tools() -> None:
    names = {tool["function"]["name"] for tool in get_tool_spec("researcher")}

    assert "write_file" not in names
    assert "edit_file" not in names
    assert "apply_patch" not in names
    assert "run_bash" not in names
    assert "revert_file" not in names
    assert "lint_fix" not in names
    assert "format_file" not in names
    assert "read_file" in names
    assert "run_tests" in names
    assert "done" in names


def test_researcher_execution_rejects_disallowed_tool(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("x = 1\n")
    actuators = AtomicTools(tmp_path, AtomicConfig())
    call = SimpleNamespace(
        id="call-researcher-1",
        function=SimpleNamespace(
            name="edit_file",
            arguments=json.dumps({
                "path": "hello.py",
                "old_text": "x = 1",
                "new_text": "x = 2",
            }),
        ),
    )

    events: list[tuple[str, object]] = []
    with patch(
        "dgov.worker.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    ):
        result, is_done = _execute_tool_call(
            call,
            actuators,
            allowed_tools=get_allowed_tool_names("researcher"),
            role="researcher",
            turn_index=4,
            tool_index=2,
        )

    assert result == "Error: Tool edit_file is not allowed in this worker role."
    assert is_done is False
    assert (tmp_path / "hello.py").read_text() == "x = 1\n"
    call_event = events[0][1]
    result_event = events[1][1]
    assert isinstance(call_event, dict)
    assert call_event["tool"] == "edit_file"
    assert call_event["role"] == "researcher"
    assert call_event["turn_index"] == 4
    assert call_event["tool_index"] == 2
    assert call_event["call_id"] == "call-researcher-1"
    assert isinstance(result_event, dict)
    assert result_event["status"] == "failed"
    assert result_event["error_kind"] == "policy_blocked"
    assert result_event["duration_ms"] >= 0


def test_run_researcher_uses_configured_iteration_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    events: list[tuple[str, object]] = []
    call_count = 0

    class _FakeMessage:
        content = None

        def __init__(self) -> None:
            self.tool_calls = []

        def model_dump(self, exclude_none: bool = True):
            return {"role": "assistant"}

    class _FakeOpenAI:
        def __init__(self, *args, **kwargs):
            completions = SimpleNamespace(create=self._create)
            self.chat = SimpleNamespace(completions=completions)

        def _create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=_FakeMessage(), finish_reason="length")]
            )

    monkeypatch.setattr("dgov.researcher.OpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        "dgov.researcher.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    with pytest.raises(SystemExit) as excinfo:
        run_researcher(
            "investigate",
            tmp_path,
            "test-model",
            json.dumps({"worker_iteration_budget": 2}),
        )

    assert excinfo.value.code == 1
    assert call_count == 2
    assert events[-1] == ("error", "Exceeded max iterations (2)")
