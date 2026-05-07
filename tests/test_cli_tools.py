"""Tests for dgov tools CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import emit_event

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


def _emit_tool_call(root: Path, tool: str, *, status: str = "success") -> None:
    emit_event(
        str(root),
        event="worker_log",
        pane="pane-a",
        plan_name="plan-a",
        task_slug="task-a",
        log_type="call",
        content={"tool": tool, "role": "worker"},
    )
    emit_event(
        str(root),
        event="worker_log",
        pane="pane-a",
        plan_name="plan-a",
        task_slug="task-a",
        log_type="result",
        content={
            "tool": tool,
            "role": "worker",
            "status": status,
            "result_chars": 25,
            "raw_result_chars": 25,
            "duration_ms": 5.0,
            "error_kind": "not_found" if status == "failed" else None,
        },
    )


def test_tools_audit_renders_human_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _emit_tool_call(root, "read_file")
        _emit_tool_call(root, "edit_file", status="failed")

        result = runner.invoke(cli, ["tools", "audit", "--plan", "plan-a"])

    assert result.exit_code == 0, result.output
    assert "Tool audit" in result.output
    assert "calls: 2" in result.output
    assert "failures: 1" in result.output
    assert "edit_file" in result.output
    assert "not_found" in result.output


def test_tools_audit_renders_json_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _emit_tool_call(root, "read_file")

        result = runner.invoke(cli, ["--json", "tools", "audit", "--plan", "plan-a"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_calls"] == 1
    assert payload["total_failures"] == 0
    assert payload["tools"][0]["tool"] == "read_file"
    assert payload["tools"][0]["average_duration_ms"] == 5.0


def test_tools_audit_empty_state(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["tools", "audit"])

    assert result.exit_code == 0, result.output
    assert "No worker tool-call events found." in result.output
