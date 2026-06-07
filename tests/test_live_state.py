"""Unit tests for event-derived live state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.live_state import is_timeout_error, live_plan_names, state_from_event, tasks_from_events
from dgov.persistence import emit_event
from dgov.types import TaskState

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "error",
    [
        "Timed out after 60s",
        "Fork timed out after 60s",
        "Worker timed out after 30s",
        "Wall-clock timeout after 60s",
        "Read timeout occurred",
    ],
)
def test_is_timeout_error_matches_runner_timeout_phrasings(error: str) -> None:
    """The runner emits both 'timed out' and 'timeout' phrasings; both must classify."""
    assert is_timeout_error(error) is True


@pytest.mark.parametrize("error", ["", None, "merge conflict", "review_hook_fail"])
def test_is_timeout_error_rejects_non_timeout_errors(error: str | None) -> None:
    assert is_timeout_error(error) is False


def test_state_from_event_classifies_timed_out_phrasing_as_timed_out() -> None:
    """Regression: 'Timed out after Xs' is what _emit_worker_terminal_event re-emits.

    Before the fix, a substring check for 'timeout' missed 'timed out' and these
    task_failed events were misclassified as TaskState.FAILED.
    """
    event = {"event": "task_failed", "error": "Timed out after 60s"}
    assert state_from_event(event) is TaskState.TIMED_OUT


def test_state_from_event_classifies_fork_timeout_as_timed_out() -> None:
    event = {"event": "task_failed", "error": "Fork timed out after 60s"}
    assert state_from_event(event) is TaskState.TIMED_OUT


def test_state_from_event_classifies_non_timeout_failure_as_failed() -> None:
    event = {"event": "task_failed", "error": "merge conflict in src/foo.py"}
    assert state_from_event(event) is TaskState.FAILED


def test_state_from_event_maps_task_closed_to_closed() -> None:
    event = {"event": "task_closed", "reason": "stale_review_attention"}
    assert state_from_event(event) is TaskState.CLOSED


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


def test_tasks_from_events_all_history_marks_unterminated_completed_run_stale(
    tmp_path: Path,
) -> None:
    """Historical output must not describe completed plan activity as still active."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )
    emit_event(str(tmp_path), "run_completed", "run-a-1", plan_name="plan-a")

    tasks = tasks_from_events(str(tmp_path), latest_run_only=False)

    assert tasks == [{"slug": "task-a", "state": "stale", "plan_name": "plan-a"}]


def test_tasks_from_events_all_history_marks_reviewed_completed_run_stale(
    tmp_path: Path,
) -> None:
    """Reviewed attention is live only while the corresponding run is open."""
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
    emit_event(str(tmp_path), "run_completed", "run-a-1", plan_name="plan-a")

    tasks = tasks_from_events(str(tmp_path), latest_run_only=False)

    assert tasks == [{"slug": "task-a", "state": "stale", "plan_name": "plan-a"}]


def test_tasks_from_events_latest_run_ignores_reviewed_completed_run(tmp_path: Path) -> None:
    """Default live state should not keep reviewed tasks after run completion."""
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
    emit_event(str(tmp_path), "run_completed", "run-a-1", plan_name="plan-a")

    assert tasks_from_events(str(tmp_path), latest_run_only=True) == []


def test_tasks_from_events_all_history_marks_superseded_run_stale(tmp_path: Path) -> None:
    """A newer run boundary supersedes unterminated historical task state."""
    emit_event(str(tmp_path), "run_start", "run-a-1", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="task-a",
    )
    emit_event(str(tmp_path), "run_start", "run-a-2", plan_name="plan-a")

    tasks = tasks_from_events(str(tmp_path), latest_run_only=False)

    assert tasks == [{"slug": "task-a", "state": "stale", "plan_name": "plan-a"}]


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


def test_live_plan_names_includes_review_outcomes(tmp_path: Path) -> None:
    """Reviewed states are live until merged or failed."""
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

    assert live_plan_names(str(tmp_path)) == {"plan-a", "plan-b"}
