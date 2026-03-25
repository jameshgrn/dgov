"""Unit tests for DAG override commands (force-complete, skip-task)."""

from __future__ import annotations

import json

import pytest

from dgov.persistence import _get_db, ensure_dag_tables, get_dag_run, read_events


def _setup_dag_run(session_root, run_id=1, tasks=None):
    """Helper to create a minimal DAG run in the DB."""
    from datetime import datetime, timezone

    ensure_dag_tables(session_root)
    conn = _get_db(session_root)
    task_states = tasks or {"task-a": "waiting", "task-b": "dispatched"}
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


@pytest.mark.unit
def test_force_complete_marks_all_tasks(tmp_path):
    from dgov.executor import run_force_complete_dag

    sr = str(tmp_path)
    _setup_dag_run(sr)
    result = run_force_complete_dag(sr, 1)
    assert result["status"] == "completed"
    assert set(result["forced"]) == {"task-a", "task-b"}
    run = get_dag_run(sr, 1)
    assert run["status"] == "completed"


@pytest.mark.unit
def test_force_complete_emits_events(tmp_path):
    from dgov.executor import run_force_complete_dag

    sr = str(tmp_path)
    _setup_dag_run(sr)
    run_force_complete_dag(sr, 1)
    events = read_events(sr)
    event_types = [e["event"] for e in events]
    assert "dag_completed" in event_types
    assert "evals_verified" in event_types


@pytest.mark.unit
def test_force_complete_not_found(tmp_path):
    from dgov.executor import run_force_complete_dag

    sr = str(tmp_path)
    ensure_dag_tables(sr)
    result = run_force_complete_dag(sr, 999)
    assert "error" in result


@pytest.mark.unit
def test_skip_task_marks_skipped(tmp_path):
    from dgov.executor import run_skip_dag_task

    sr = str(tmp_path)
    _setup_dag_run(sr)
    result = run_skip_dag_task(sr, 1, "task-a")
    assert result["status"] == "skipped"
    run = get_dag_run(sr, 1)
    assert run["state_json"]["task_states"]["task-a"] == "skipped"


@pytest.mark.unit
def test_skip_task_emits_event(tmp_path):
    from dgov.executor import run_skip_dag_task

    sr = str(tmp_path)
    _setup_dag_run(sr)
    run_skip_dag_task(sr, 1, "task-a")
    events = read_events(sr)
    event_types = [e["event"] for e in events]
    assert "dag_task_completed" in event_types


@pytest.mark.unit
def test_skip_task_already_terminal(tmp_path):
    from dgov.executor import run_skip_dag_task

    sr = str(tmp_path)
    _setup_dag_run(sr, tasks={"task-a": "merged", "task-b": "waiting"})
    result = run_skip_dag_task(sr, 1, "task-a")
    assert "error" in result


@pytest.mark.unit
def test_skip_task_not_found(tmp_path):
    from dgov.executor import run_skip_dag_task

    sr = str(tmp_path)
    _setup_dag_run(sr)
    result = run_skip_dag_task(sr, 1, "nonexistent")
    assert "error" in result
