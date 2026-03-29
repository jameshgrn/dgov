"""Tests for DAG post-mortem failure context query."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dgov.persistence import (
    _get_db,
    create_dag_run,
    emit_event,
    ensure_dag_tables,
    get_dag_task_failure_context,
)

pytestmark = pytest.mark.unit


def test_failure_context_returns_last_event(tmp_path):
    """Assert last event is returned for a failed task."""
    sr = str(tmp_path)
    ensure_dag_tables(sr)
    db = _get_db(sr)

    # Insert dag_run with partial status
    run_id = create_dag_run(
        sr,
        dag_file="/tmp/test.dag",
        started_at=datetime.now(timezone.utc).isoformat(),
        status="partial",
        current_tier=1,
        state_json={"units": []},
    )

    # Insert dag_task with failed status
    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-a", "failed", "pi", "pane-a"),
    )
    db.commit()

    # Add multiple events for the pane
    emit_event(sr, "pane_created", "pane-a")
    emit_event(sr, "pane_done", "pane-a", commit_count="3")
    emit_event(sr, "pane_failed", "pane-a", error="build failed")

    result = get_dag_task_failure_context(sr, run_id)

    assert "task-a" in result
    assert result["task-a"]["last_event"] == "pane_failed"
    assert result["task-a"]["pane_slug"] == "pane-a"
    assert result["task-a"]["error"] == "build failed"
    assert result["task-a"]["last_event_ts"] is not None


def test_failure_context_extracts_verdict(tmp_path):
    """Assert verdict is extracted from review event payload."""
    sr = str(tmp_path)
    ensure_dag_tables(sr)
    db = _get_db(sr)

    run_id = create_dag_run(
        sr,
        dag_file="/tmp/test.dag",
        started_at=datetime.now(timezone.utc).isoformat(),
        status="partial",
        current_tier=1,
        state_json={"units": []},
    )

    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-b", "failed", "pi", "pane-b"),
    )
    db.commit()

    # Add review event with verdict in payload
    emit_event(sr, "review_fail", "pane-b", verdict="unsafe", reason="lint errors")

    result = get_dag_task_failure_context(sr, run_id)

    assert "task-b" in result
    assert result["task-b"]["verdict"] == "unsafe"
    assert result["task-b"]["last_event"] == "review_fail"


def test_failure_context_excludes_merged(tmp_path):
    """Assert only failed tasks appear in results, merged tasks excluded."""
    sr = str(tmp_path)
    ensure_dag_tables(sr)
    db = _get_db(sr)

    run_id = create_dag_run(
        sr,
        dag_file="/tmp/test.dag",
        started_at=datetime.now(timezone.utc).isoformat(),
        status="partial",
        current_tier=1,
        state_json={"units": []},
    )

    # Insert merged task
    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-merged", "merged", "pi", "pane-merged"),
    )
    # Insert failed task
    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-failed", "failed", "pi", "pane-failed"),
    )
    db.commit()

    emit_event(sr, "pane_merged", "pane-merged", merge_sha="abc123")
    emit_event(sr, "pane_failed", "pane-failed", error="test failure")

    result = get_dag_task_failure_context(sr, run_id)

    assert "task-failed" in result
    assert "task-merged" not in result
    assert result["task-failed"]["last_event"] == "pane_failed"


def test_failure_context_empty_for_clean(tmp_path):
    """Assert empty dict returned when all tasks are merged."""
    sr = str(tmp_path)
    ensure_dag_tables(sr)
    db = _get_db(sr)

    run_id = create_dag_run(
        sr,
        dag_file="/tmp/test.dag",
        started_at=datetime.now(timezone.utc).isoformat(),
        status="completed",
        current_tier=2,
        state_json={"units": []},
    )

    # Insert only merged tasks
    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-1", "merged", "pi", "pane-1"),
    )
    db.execute(
        "INSERT INTO dag_tasks (dag_run_id, slug, status, agent, pane_slug)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "task-2", "merged", "pi", "pane-2"),
    )
    db.commit()

    emit_event(sr, "pane_merged", "pane-1")
    emit_event(sr, "pane_merged", "pane-2")

    result = get_dag_task_failure_context(sr, run_id)

    assert result == {}
