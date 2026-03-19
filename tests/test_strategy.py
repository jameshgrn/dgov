"""Unit tests for task context extraction from prompts."""

from __future__ import annotations

import pytest

from dgov.strategy import extract_task_context

pytestmark = pytest.mark.unit


class TestExtractTaskContext:
    def test_merge_review_domain(self) -> None:
        result = extract_task_context("Inspect the verdict for this rebase operation")
        assert "src/dgov/merger.py" in result["primary_files"]
        assert "src/dgov/inspection.py" in result["primary_files"]
        assert "src/dgov/persistence.py" in result["also_check"]
        assert any("test_merger" in t for t in result["tests"])
        assert any("verdict" in h.lower() or "merge" in h.lower() for h in result["hints"])

    def test_cli_command_domain(self) -> None:
        result = extract_task_context("Add a new cli command to the CLI")
        assert "src/dgov/cli/__init__.py" in result["primary_files"]
        assert "src/dgov/cli/pane.py" in result["also_check"]
        assert any("test_cli" in t or "test_dgov_cli" in t for t in result["tests"])

    def test_retry_escalation_domain(self) -> None:
        result = extract_task_context("Handle escalation chain when worker fails")
        assert "src/dgov/recovery.py" in result["primary_files"]
        assert "src/dgov/responder.py" in result["also_check"]
        assert any("test_retry" in t for t in result["tests"])

    def test_monitor_daemon_domain(self) -> None:
        result = extract_task_context("Set up watchdog daemon with TOML hooks")
        assert "src/dgov/monitor.py" in result["primary_files"]
        assert "src/dgov/monitor_hooks.py" in result["also_check"]
        assert "tests/test_monitor.py" in result["tests"]

    def test_worker_done_domain(self) -> None:
        result = extract_task_context("Worker done signal after checkpoint")
        assert "src/dgov/cli/worker_cmd.py" in result["primary_files"]
        assert "src/dgov/done.py" in result["primary_files"]
        assert "src/dgov/waiter.py" in result["also_check"]
        assert any("test_done" in t for t in result["tests"])

    def test_no_match_returns_empty_lists(self) -> None:
        result = extract_task_context("Make the sky blue")
        assert result["primary_files"] == []
        assert result["also_check"] == []
        assert result["tests"] == []
        assert result["hints"] == []

    def test_case_insensitive_matching(self) -> None:
        result_upper = extract_task_context("FIX MERGE CONFLICT NOW")
        result_lower = extract_task_context("fix merge conflict now")
        assert result_upper["primary_files"] == result_lower["primary_files"]
        assert result_upper["also_check"] == result_lower["also_check"]
