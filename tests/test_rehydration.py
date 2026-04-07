"""Tests for event-sourced rehydration in EventDagRunner."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.persistence import clear_connection_cache, emit_event
from dgov.runner import EventDagRunner
from dgov.types import TaskState


@pytest.fixture
def mock_dag() -> DagDefinition:
    return DagDefinition(
        name="test-plan",
        dag_file="dummy.toml",
        tasks={
            "a": DagTaskSpec(
                slug="a",
                summary="A",
                prompt="A",
                commit_message="A",
                agent="agent",
                files=DagFileSpec(create=("a.py",)),
            ),
            "b": DagTaskSpec(
                slug="b",
                summary="B",
                prompt="B",
                commit_message="B",
                agent="agent",
                depends_on=("a",),
                files=DagFileSpec(create=("b.py",)),
            ),
        },
    )


@pytest.mark.unit
def test_rehydration_restores_kernel_state(tmp_path: Path, mock_dag: DagDefinition, monkeypatch):
    """EventDagRunner should restore kernel state from the event log on init."""
    session_root = str(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    clear_connection_cache()

    # 1. Simulate prior progress by emitting events directly
    # Task 'a' was dispatched, finished, passed review, and merged.
    emit_event(session_root, "dag_task_dispatched", "p1", plan_name="test-plan", task_slug="a")
    emit_event(session_root, "task_done", "p1", plan_name="test-plan", task_slug="a")
    emit_event(
        session_root, "review_pass", "p1", plan_name="test-plan", task_slug="a", commit_count=1
    )
    emit_event(
        session_root,
        "merge_completed",
        "p1",
        plan_name="test-plan",
        task_slug="a",
        merge_sha="abc",
    )

    # 2. Initialize runner (rehydration happens in __init__)
    runner = EventDagRunner(mock_dag, session_root=session_root)

    # 3. Verify kernel state
    assert runner.kernel.task_states["a"] == TaskState.MERGED
    assert runner.kernel.task_states["b"] == TaskState.PENDING

    # 4. Verify runner start() unblocks 'b'
    actions = runner.kernel.start()
    from dgov.actions import DispatchTask

    assert any(isinstance(a, DispatchTask) and a.task_slug == "b" for a in actions)


@pytest.mark.unit
def test_rehydration_restores_attempts(tmp_path: Path, mock_dag: DagDefinition, monkeypatch):
    """EventDagRunner should restore task attempt counts via governor_resumed events."""
    session_root = str(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    clear_connection_cache()

    # Task 'a' failed once and was retried
    emit_event(session_root, "dag_task_dispatched", "p1", plan_name="test-plan", task_slug="a")
    emit_event(
        session_root, "task_failed", "p1", plan_name="test-plan", task_slug="a", error="fail"
    )
    emit_event(
        session_root,
        "dag_task_governor_resumed",
        "p1",
        plan_name="test-plan",
        task_slug="a",
        action="retry",
    )

    runner = EventDagRunner(mock_dag, session_root=session_root)

    assert runner.kernel.task_states["a"] == TaskState.PENDING
    assert runner.kernel.attempts["a"] == 1
