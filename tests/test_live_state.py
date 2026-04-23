"""Unit tests for event-derived live state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.live_state import live_plan_names, tasks_from_events
from dgov.persistence import emit_event

pytestmark = pytest.mark.unit


def test_tasks_from_events_scopes_each_plan_to_latest_run_start(tmp_path: Path) -> None:
    """latest_run_only must respect the latest run boundary per plan, not globally."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a-old",
        plan_name="plan-a",
        task_slug="old-a",
    )
    emit_event(str(tmp_path), "run_start", "run-b-1", plan_name="plan-b")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-b",
        plan_name="plan-b",
        task_slug="current-b",
    )
    emit_event(str(tmp_path), "run_start", "run-a-2", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a-new",
        plan_name="plan-a",
        task_slug="current-a",
    )

    tasks = tasks_from_events(str(tmp_path), latest_run_only=True)

    assert tasks == [
        {"slug": "current-a", "state": "active", "plan_name": "plan-a"},
        {"slug": "current-b", "state": "active", "plan_name": "plan-b"},
    ]


def test_tasks_from_events_all_history_keeps_latest_state_per_task(tmp_path: Path) -> None:
    """The history view should keep the latest state reached for each task slug."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )
    emit_event(str(tmp_path), "task_done", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(str(tmp_path), "review_pass", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(
        str(tmp_path),
        "merge_completed",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )

    tasks = tasks_from_events(str(tmp_path), latest_run_only=False)

    assert tasks == [{"slug": "task-a", "state": "merged", "plan_name": "plan-a"}]


def test_live_plan_names_ignores_stale_prior_runs(tmp_path: Path) -> None:
    """A newer run_start with no activity should hide active tasks from an older run."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )
    emit_event(str(tmp_path), "run_start", "run-a-2", plan_name="plan-a")

    assert live_plan_names(str(tmp_path)) == set()


def test_live_plan_names_ignores_review_outcomes(tmp_path: Path) -> None:
    """Review outcomes are historical results, not live plan activity."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )
    emit_event(str(tmp_path), "task_done", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(str(tmp_path), "review_fail", "pane-a", plan_name="plan-a", task_slug="task-a")

    emit_event(str(tmp_path), "run_start", "run-b-1", plan_name="plan-b")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-b",
        plan_name="plan-b",
        task_slug="task-b",
    )
    emit_event(str(tmp_path), "task_done", "pane-b", plan_name="plan-b", task_slug="task-b")
    emit_event(str(tmp_path), "review_pass", "pane-b", plan_name="plan-b", task_slug="task-b")

    assert live_plan_names(str(tmp_path)) == set()
