"""Tests for dgov.scope_status parity with settlement scope checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.persistence.events import emit_event
from dgov.scope_status import analyze_scope_status


def _emit_worker_result_activity(
    session_root: Path,
    pane_slug: str,
    task_slug: str,
    tool: str,
    path: str,
    mode: str,
) -> None:
    """Emit a worker_log event with a single activity entry."""
    emit_event(
        str(session_root),
        "worker_log",
        pane_slug,
        plan_name="plan",
        task_slug=task_slug,
        log_type="result",
        content={
            "tool": tool,
            "status": "success",
            "activity": [{"kind": tool, "path": path, "mode": mode}],
        },
    )


@pytest.mark.unit
class TestAnalyzeScopeStatus:
    def test_unclaimed_modified_files(self) -> None:
        actual = frozenset({"claimed.py", "unclaimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
        )
        assert status.claimed_writable == frozenset({"claimed.py"})
        assert status.unclaimed_actual_paths == frozenset({"unclaimed.py"})
        assert status.blocking_failure is not None
        assert status.blocking_failure.verdict == "scope_violation"
        assert "unclaimed.py" in (status.blocking_failure.error or "")

    def test_edited_read_only_files(self) -> None:
        actual = frozenset({"claimed.py", "readonly.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            read_files=["readonly.py"],
        )
        assert status.claimed_readonly == frozenset({"readonly.py"})
        assert status.unclaimed_actual_paths == frozenset({"readonly.py"})
        assert status.blocking_failure is not None
        assert status.blocking_failure.verdict == "read_scope_violation"
        assert "readonly.py" in (status.blocking_failure.error or "")

    def test_ignored_lockfile(self) -> None:
        actual = frozenset({"claimed.py", "uv.lock"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            scope_ignore_files=("uv.lock",),
        )
        assert status.ignored_actual_paths == frozenset({"uv.lock"})
        assert status.unclaimed_actual_paths == frozenset()
        assert status.blocking_failure is None

    def test_ignored_named_dir_matches_nested(self) -> None:
        actual = frozenset({"claimed.py", "pkg/__pycache__/claimed.cpython-312.pyc"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            scope_ignore_files=("__pycache__",),
        )
        assert status.ignored_actual_paths == frozenset({
            "pkg/__pycache__/claimed.cpython-312.pyc"
        })
        assert status.unclaimed_actual_paths == frozenset()
        assert status.blocking_failure is None

    def test_ignored_glob_matches_pyc(self) -> None:
        actual = frozenset({"claimed.py", "scratch.pyc"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            scope_ignore_files=("*.pyc",),
        )
        assert status.ignored_actual_paths == frozenset({"scratch.pyc"})
        assert status.unclaimed_actual_paths == frozenset()
        assert status.blocking_failure is None

    def test_clean_in_scope_changes(self) -> None:
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
        )
        assert status.claimed_writable == frozenset({"claimed.py"})
        assert status.unclaimed_actual_paths == frozenset()
        assert status.blocking_failure is None

    def test_no_scope_check_without_claims(self) -> None:
        actual = frozenset({"anything.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=None,
        )
        assert status.claimed_writable == frozenset()
        assert status.unclaimed_actual_paths == frozenset({"anything.py"})
        assert status.blocking_failure is None

    def test_transient_unclaimed_tool_write(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        _emit_worker_result_activity(
            session_root, "pane-1", "task-1", "write_file", "scratch.py", "create"
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            session_root=str(session_root),
            task_slug="task-1",
        )
        assert status.transient_write_paths == frozenset({"scratch.py"})
        assert status.unclaimed_transient_paths == frozenset({"scratch.py"})
        assert status.blocking_failure is not None
        assert status.blocking_failure.verdict == "scope_violation"
        assert "scratch.py" in (status.blocking_failure.error or "")

    def test_transient_claimed_tool_write(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        _emit_worker_result_activity(
            session_root, "pane-1", "task-1", "edit_file", "claimed.py", "edit"
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            session_root=str(session_root),
            task_slug="task-1",
        )
        assert status.transient_write_paths == frozenset({"claimed.py"})
        assert status.unclaimed_transient_paths == frozenset()
        assert status.blocking_failure is None

    def test_transient_scope_ignores_other_panes(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        _emit_worker_result_activity(
            session_root, "pane-old", "task-1", "write_file", "scratch.py", "create"
        )
        _emit_worker_result_activity(
            session_root, "pane-current", "task-1", "edit_file", "claimed.py", "edit"
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            session_root=str(session_root),
            task_slug="task-1",
            pane_slug="pane-current",
        )
        assert status.transient_write_paths == frozenset({"claimed.py"})
        assert status.unclaimed_transient_paths == frozenset()
        assert status.blocking_failure is None

    def test_transient_read_only_activity_ignored(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "read_file",
                "status": "success",
                "activity": [
                    {"kind": "read_file", "path": "unclaimed_context.py"},
                    {"kind": "edit_file", "path": "claimed.py", "mode": "edit"},
                ],
            },
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            session_root=str(session_root),
            task_slug="task-1",
        )
        assert status.transient_write_paths == frozenset({"claimed.py"})
        assert status.unclaimed_transient_paths == frozenset()
        assert status.blocking_failure is None

    def test_transient_unclaimed_write_with_read_only_mixed(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "edit_file",
                "status": "success",
                "activity": [
                    {"kind": "read_file", "path": "unclaimed_context.py"},
                    {"kind": "write_file", "path": "scratch.py", "mode": "create"},
                    {"kind": "edit_file", "path": "claimed.py", "mode": "edit"},
                ],
            },
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            session_root=str(session_root),
            task_slug="task-1",
        )
        assert status.transient_write_paths == frozenset({"scratch.py", "claimed.py"})
        assert status.unclaimed_transient_paths == frozenset({"scratch.py"})
        assert status.blocking_failure is not None
        assert status.blocking_failure.verdict == "scope_violation"
        assert "scratch.py" in (status.blocking_failure.error or "")

    def test_ignored_transient_lockfile(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        _emit_worker_result_activity(
            session_root, "pane-1", "task-1", "write_file", "uv.lock", "create"
        )
        actual = frozenset({"claimed.py"})
        status = analyze_scope_status(
            actual_files=actual,
            claimed_files=["claimed.py"],
            scope_ignore_files=("uv.lock",),
            session_root=str(session_root),
            task_slug="task-1",
        )
        assert status.transient_write_paths == frozenset({"uv.lock"})
        assert status.ignored_transient_paths == frozenset({"uv.lock"})
        assert status.unclaimed_transient_paths == frozenset()
        assert status.blocking_failure is None
