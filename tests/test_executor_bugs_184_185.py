"""Regression tests for bug #184 and bug #185."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dgov.persistence import (
    WorkerPane,
    _get_db,
    add_pane,
    ensure_dag_tables,
)

pytestmark = pytest.mark.unit


def _setup_dag_run(session_root: str, run_id: int = 1, tasks: dict | None = None) -> None:
    """Helper to create a minimal DAG run in the DB."""
    import json
    from datetime import datetime, timezone

    ensure_dag_tables(session_root)
    conn = _get_db(session_root)
    task_states = tasks or {"task-a": "waiting"}
    state_json = {
        "deps": {k: () for k in task_states},
        "state": "running",
        "task_states": task_states,
        "pane_slugs": {},
        "attempts": {k: 1 for k in task_states},
        "merge_order": list(task_states.keys()),
        "merge_cursor": 0,
    }
    conn.execute(
        "INSERT INTO dag_runs (id, dag_file, started_at, status, current_tier, state_json,"
        " definition_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "test.toml",
            datetime.now(timezone.utc).isoformat(),
            "running",
            0,
            json.dumps(state_json),
            json.dumps({}),
        ),
    )
    conn.commit()


class TestBug184CancelRetryDescendants:
    """Bug #184: Cancelled DAG runs leave retry descendants alive."""

    def test_cancel_dag_closes_retry_descendants(self, tmp_path, monkeypatch):
        """When cancelling a DAG run, retry descendant panes must also be closed."""
        from dgov.executor import run_cancel_dag

        sr = str(tmp_path)
        _setup_dag_run(sr, tasks={"task-a": "waiting"})
        conn = _get_db(sr)
        conn.execute(
            "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, attempt, pane_slug, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "task-a", "waiting", "worker", 1, "pane-a", None),
        )
        conn.commit()

        # Add original pane and retry descendants
        import time

        add_pane(
            sr,
            WorkerPane(
                slug="pane-a",
                prompt="test",
                pane_id="%1",
                agent="worker",
                project_root=str(tmp_path),
                worktree_path=str(tmp_path / "pane-a"),
                branch_name="pane-a",
                role="worker",
                created_at=time.time(),
            ),
        )
        add_pane(
            sr,
            WorkerPane(
                slug="pane-a-retry",
                prompt="test retry",
                pane_id="%2",
                agent="worker",
                project_root=str(tmp_path),
                worktree_path=str(tmp_path / "pane-a-retry"),
                branch_name="pane-a-retry",
                role="worker",
                parent_slug="pane-a",
                created_at=time.time(),
            ),
        )
        add_pane(
            sr,
            WorkerPane(
                slug="pane-a-retry-2",
                prompt="test retry 2",
                pane_id="%3",
                agent="worker",
                project_root=str(tmp_path),
                worktree_path=str(tmp_path / "pane-a-retry-2"),
                branch_name="pane-a-retry-2",
                role="worker",
                parent_slug="pane-a-retry",
                created_at=time.time(),
            ),
        )

        # Track which panes get closed
        closed = []
        monkeypatch.setattr(
            "dgov.persistence.get_pane",
            lambda session_root, slug: {
                "project_root": str(tmp_path),
                "slug": slug,
            },
        )
        monkeypatch.setattr(
            "dgov.lifecycle.close_worker_pane",
            lambda project_root, slug, session_root=None, force=False: closed.append(slug) or True,
        )

        result = run_cancel_dag(sr, 1)

        assert result["status"] == "cancelled"
        # All descendants including retry chain should be closed
        assert "pane-a" in closed
        assert "pane-a-retry" in closed
        assert "pane-a-retry-2" in closed


class TestBug185TimeoutEvent:
    """Bug #185: Worker plan tasks stall in read-only phase without emitting timeout events."""

    @patch("dgov.monitor.observe_worker")
    @patch("dgov.persistence.emit_event")
    def test_dag_wait_emits_timeout_event(self, mock_emit, mock_observe, tmp_path, monkeypatch):
        """When DAG wait times out, a pane_timed_out event must be emitted."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import WorkerPhase
        from dgov.monitor import WorkerObservation

        sr = str(tmp_path)

        # Mock observation showing worker stuck in STUCK phase
        mock_observe.return_value = WorkerObservation(
            slug="pane-1",
            phase=WorkerPhase.STUCK,
            alive=True,
            has_commits=False,
            has_done_signal=False,
            has_exit_signal=False,
            exit_code=None,
            classification="unknown",
            reason="stuck",
        )

        # Mock time to simulate timeout
        call_count = [0]

        def mock_time():
            call_count[0] += 1
            if call_count[0] < 3:
                return 0.0
            return 1000.0  # Timeout after 3rd call

        monkeypatch.setattr("time.monotonic", mock_time)

        # Add project_root to make full path
        pr = str(tmp_path)
        result = _dag_wait_any(
            pr,
            sr,
            ("task-1",),
            {"task-1": "pane-1"},
            {"task-1": {}},
            {"task-1": 1},  # 1 second timeout
            0.1,  # poll interval
            0.5,  # readonly timeout
        )

        assert result.pane_state == "timed_out"
        # Bug #185 fix: pane_timed_out event must be emitted
        assert mock_emit.called
        call_args = mock_emit.call_args
        assert call_args[0][0] == sr
        assert call_args[0][1] == "pane_timed_out"
