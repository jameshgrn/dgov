"""Unit tests for WatchSession — mode, cursor, and plan-switch logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.persistence import emit_event
from dgov.watch_state import EventRowUpdate, PlanSwitchUpdate, WatchSession

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _event_rows(updates: list) -> list[EventRowUpdate]:
    return [u for u in updates if isinstance(u, EventRowUpdate)]


def _switches(updates: list) -> list[tuple[str | None, str | None]]:
    return [(u.from_plan, u.to_plan) for u in updates if isinstance(u, PlanSwitchUpdate)]


def _plan_names(updates: list) -> set[str | None]:
    return {u.plan_name for u in _event_rows(updates)}


def _slugs(updates: list) -> list[str | None]:
    return [u.task_slug for u in _event_rows(updates)]


# ── follow mode ───────────────────────────────────────────────────────────────


def test_follow_switches_plan_a_to_plan_b(tmp_path: Path) -> None:
    """PlanSwitchUpdate emitted when plan A finishes and plan B becomes sole live plan."""
    root = str(tmp_path)
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="task-a")

    session = WatchSession(project_root=root, mode="follow")
    session.initialize()
    assert session.active_plan == "plan-a"
    session.poll()  # drain initial events

    emit_event(root, "task_done", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "merge_completed", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "run_completed", "run-a-1", plan_name="plan-a")
    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="task-b")

    updates = session.poll()
    assert _switches(updates) == [("plan-a", "plan-b")]


def test_follow_no_stale_plan_a_events_after_switch(tmp_path: Path) -> None:
    """After switching from A to B, polls return only plan-B events — no stale A replay."""
    root = str(tmp_path)
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="stale-a")
    emit_event(root, "task_done", "pane-a", plan_name="plan-a", task_slug="stale-a")
    emit_event(root, "merge_completed", "pane-a", plan_name="plan-a", task_slug="stale-a")
    emit_event(root, "run_completed", "run-a-1", plan_name="plan-a")
    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="fresh-b")

    session = WatchSession(project_root=root, mode="follow")
    session.initialize()
    # plan-a finished, plan-b is live → initializes directly on plan-b
    assert session.active_plan == "plan-b"

    updates = session.poll()
    assert "plan-a" not in _plan_names(updates)
    assert "fresh-b" in _slugs(updates)


# ── pinned mode ───────────────────────────────────────────────────────────────


def test_pinned_stays_pinned_ignores_new_live_plan(tmp_path: Path) -> None:
    """Pinned mode never auto-switches even when a new single live plan appears."""
    root = str(tmp_path)
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="task-a")

    session = WatchSession(project_root=root, mode="pinned", pinned_plan="plan-a")
    session.initialize()
    assert session.active_plan == "plan-a"

    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="task-b")

    updates = session.poll()
    assert _switches(updates) == []
    assert "plan-b" not in _plan_names(updates)
    assert session.active_plan == "plan-a"


def test_pinned_returns_only_pinned_plan_events(tmp_path: Path) -> None:
    """Pinned mode filters to exactly the pinned plan regardless of other active plans."""
    root = str(tmp_path)
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="task-b")

    session = WatchSession(project_root=root, mode="pinned", pinned_plan="plan-a")
    session.initialize()

    updates = session.poll()
    assert _plan_names(updates) == {"plan-a"}


# ── all mode ──────────────────────────────────────────────────────────────────


def test_all_mode_returns_events_from_all_plans(tmp_path: Path) -> None:
    """--all mode returns events from every plan with no filtering."""
    root = str(tmp_path)
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="task-b")

    session = WatchSession(project_root=root, mode="all")
    session.initialize()
    assert session.cursor == 0

    updates = session.poll()
    assert "plan-a" in _plan_names(updates)
    assert "plan-b" in _plan_names(updates)


# ── idle-to-active follow ──────────────────────────────────────────────────────


def test_follow_idle_starts_from_latest_id_then_picks_up_new_plan(tmp_path: Path) -> None:
    """Idle follow (multiple live plans) advances cursor to latest_event_id; picks up new plan."""
    root = str(tmp_path)
    # Two live plans → ambiguous → session goes idle
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "run_start", "run-b-1", plan_name="plan-b")
    emit_event(root, "dag_task_dispatched", "pane-b", plan_name="plan-b", task_slug="task-b")

    session = WatchSession(project_root=root, mode="follow")
    session.initialize()
    assert session.active_plan is None

    updates_idle = session.poll()
    assert updates_idle == []

    # Both plans complete
    emit_event(root, "task_done", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "merge_completed", "pane-a", plan_name="plan-a", task_slug="task-a")
    emit_event(root, "run_completed", "run-a-1", plan_name="plan-a")
    emit_event(root, "task_done", "pane-b", plan_name="plan-b", task_slug="task-b")
    emit_event(root, "merge_completed", "pane-b", plan_name="plan-b", task_slug="task-b")
    emit_event(root, "run_completed", "run-b-1", plan_name="plan-b")

    # Single new plan starts
    emit_event(root, "run_start", "run-c-1", plan_name="plan-c")
    emit_event(root, "dag_task_dispatched", "pane-c", plan_name="plan-c", task_slug="task-c")

    updates = session.poll()
    assert _switches(updates) == [(None, "plan-c")]
    assert "plan-c" in _plan_names(updates)
    assert "plan-a" not in _plan_names(updates)
    assert "plan-b" not in _plan_names(updates)


# ── stale run suppression ──────────────────────────────────────────────────────


def test_stale_prior_run_suppressed_for_plan_with_multiple_run_starts(tmp_path: Path) -> None:
    """Cursor initializes at latest run_start id so prior-run events are not replayed."""
    root = str(tmp_path)
    # First run of plan-a
    emit_event(root, "run_start", "run-a-1", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="stale-task")
    emit_event(root, "task_done", "pane-a", plan_name="plan-a", task_slug="stale-task")
    # Second run of plan-a (restart)
    emit_event(root, "run_start", "run-a-2", plan_name="plan-a")
    emit_event(root, "dag_task_dispatched", "pane-a", plan_name="plan-a", task_slug="fresh-task")

    session = WatchSession(project_root=root, mode="pinned", pinned_plan="plan-a")
    session.initialize()

    updates = session.poll()
    slugs = _slugs(updates)
    assert "stale-task" not in slugs
    assert "fresh-task" in slugs
