"""Unit tests for DAG override commands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

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


@pytest.mark.unit
def test_cancel_dag_marks_nonterminal_tasks_cancelled(tmp_path, monkeypatch):
    from dgov.executor import run_cancel_dag

    sr = str(tmp_path)
    _setup_dag_run(
        sr, tasks={"task-a": "waiting", "task-b": "merged", "task-c": "blocked_on_governor"}
    )
    conn = _get_db(sr)
    conn.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, attempt, pane_slug, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "task-a", "waiting", "worker", 1, "pane-a", None),
    )
    conn.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, attempt, pane_slug, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "task-b", "merged", "worker", 1, "pane-b", None),
    )
    conn.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, attempt, pane_slug, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "task-c", "blocked_on_governor", "worker", 2, "pane-c", "review_failed"),
    )
    conn.commit()

    monkeypatch.setattr(
        "dgov.persistence.get_pane",
        lambda session_root, slug: {"project_root": "/repo", "slug": slug},
    )
    closed = []
    monkeypatch.setattr(
        "dgov.lifecycle.close_worker_pane",
        lambda project_root, slug, session_root=None, force=False: (
            closed.append((project_root, slug, force)) or True
        ),
    )

    result = run_cancel_dag(sr, 1)

    assert result["status"] == "cancelled"
    assert set(result["cancelled"]) == {"task-a", "task-c"}
    assert closed == [
        ("/repo", "pane-a", True),
        ("/repo", "pane-b", True),
        ("/repo", "pane-c", True),
    ]
    run = get_dag_run(sr, 1)
    assert run["status"] == "cancelled"
    assert run["state_json"]["state"] == "cancelled"
    assert run["state_json"]["task_states"]["task-a"] == "cancelled"
    assert run["state_json"]["task_states"]["task-b"] == "merged"
    assert run["state_json"]["task_states"]["task-c"] == "cancelled"
    event_types = [e["event"] for e in read_events(sr)]
    assert "dag_cancelled" in event_types


@pytest.mark.unit
def test_dag_cancel_command_uses_latest_open_run(tmp_path, monkeypatch):
    from dgov.cli.dag_cmd import dag

    dagfile = tmp_path / "test.toml"
    dagfile.write_text("name = 'x'\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.ensure_dag_tables", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "dgov.persistence.get_open_dag_run",
        lambda session_root, abs_path: {"id": 7, "dag_file": abs_path, "status": "running"},
    )
    monkeypatch.setattr(
        "dgov.executor.run_cancel_dag",
        lambda session_root, run_id: {
            "run_id": run_id,
            "status": "cancelled",
            "cancelled": [],
            "closed": [],
        },
    )

    runner = CliRunner()
    result = runner.invoke(dag, ["cancel", str(dagfile)], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_id"] == 7
    assert payload["status"] == "cancelled"
