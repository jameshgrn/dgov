"""Event-derived live state helpers.

These helpers treat the event log as the source of truth for current governor
state. Mutable task rows are persistence/cache, not authority.
"""

from __future__ import annotations

from dgov.persistence import read_events
from dgov.types import TaskState

LIVE_STATES: frozenset[str] = frozenset({
    TaskState.PENDING,
    TaskState.ACTIVE,
    TaskState.DONE,
    TaskState.REVIEWING,
    TaskState.MERGING,
    "settling",
    TaskState.REVIEWED_PASS,
    TaskState.REVIEWED_FAIL,
})

# Settlement phases that indicate active settlement work
SETTLEMENT_PHASES = frozenset({
    "integration",
    "semantic_gate",
    "merge",
})

# Terminal states that end settlement visibility
TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.MERGED,
    TaskState.FAILED,
    TaskState.CLOSED,
    TaskState.ABANDONED,
    TaskState.TIMED_OUT,
    TaskState.SKIPPED,
})

PLAN_BOUNDARY_EVENTS = frozenset({
    "run_start",
    "run_completed",
})

_EVENT_STATE_MAP: dict[str, TaskState] = {
    "dag_task_dispatched": TaskState.ACTIVE,
    "task_done": TaskState.DONE,
    "task_abandoned": TaskState.ABANDONED,
    "task_timed_out": TaskState.TIMED_OUT,
    "review_pass": TaskState.REVIEWED_PASS,
    "review_fail": TaskState.REVIEWED_FAIL,
    "merge_completed": TaskState.MERGED,
    "task_merge_failed": TaskState.FAILED,
    "task_closed": TaskState.CLOSED,
}

_GOVERNOR_RESUME_STATE_MAP: dict[str, TaskState] = {
    "retry": TaskState.PENDING,
    "skip": TaskState.SKIPPED,
    "fail": TaskState.FAILED,
}


def _task_key(event: dict) -> tuple[str, str] | None:
    task_slug = event.get("task_slug")
    if not task_slug:
        return None
    return str(event.get("plan_name") or ""), str(task_slug)


def _event_id(event: dict) -> int:
    return int(event.get("id", 0))


def state_from_event(event: dict) -> TaskState | None:
    """Map a lifecycle event to the task state it establishes."""
    event_name = event.get("event")
    if event_name == "task_failed":
        error = str(event.get("error", "")).lower()
        return TaskState.TIMED_OUT if "timeout" in error else TaskState.FAILED
    if event_name == "dag_task_governor_resumed":
        return _GOVERNOR_RESUME_STATE_MAP.get(str(event.get("action") or ""))
    return _EVENT_STATE_MAP.get(str(event_name))


def phase_from_event(event: dict) -> str | None:
    """Extract settlement phase from settlement phase events."""
    event_name = event.get("event")
    if event_name == "settlement_phase_started":
        phase = event.get("phase", "")
        if phase in SETTLEMENT_PHASES:
            return f"settling:{phase}"
        return "settling"
    if event_name == "settlement_phase_completed":
        # Phase completed - returns None to indicate phase should be cleared
        # The actual state transition is handled by merge_completed or task_merge_failed
        return None
    return None


def latest_run_start_ids(events: list[dict]) -> dict[str, int]:
    """Return the latest run_start id for each plan present in the event log."""
    latest: dict[str, int] = {}
    for event in events:
        if event.get("event") != "run_start":
            continue
        plan_name = event.get("plan_name")
        if not plan_name:
            continue
        latest[str(plan_name)] = max(latest.get(str(plan_name), 0), _event_id(event))
    return latest


def latest_run_completed_ids(events: list[dict]) -> dict[str, int]:
    """Return the latest run_completed id for each plan present in the event log."""
    latest: dict[str, int] = {}
    for event in events:
        if event.get("event") != "run_completed":
            continue
        plan_name = event.get("plan_name")
        if not plan_name:
            continue
        latest[str(plan_name)] = max(latest.get(str(plan_name), 0), _event_id(event))
    return latest


def latest_plan_boundary_ids(events: list[dict]) -> dict[str, int]:
    """Return the latest plan-level boundary id for each plan in the event log."""
    latest: dict[str, int] = {}
    for event in events:
        if event.get("event") not in PLAN_BOUNDARY_EVENTS:
            continue
        plan_name = event.get("plan_name")
        if not plan_name:
            continue
        latest[str(plan_name)] = max(latest.get(str(plan_name), 0), _event_id(event))
    return latest


def _event_is_in_latest_run_window(
    event: dict,
    *,
    latest_run_ids: dict[str, int],
    latest_completed_ids: dict[str, int],
) -> bool:
    plan_name = str(event.get("plan_name") or "")
    latest_run_id = latest_run_ids.get(plan_name, 0)
    if _event_id(event) <= latest_run_id:
        return False
    return latest_completed_ids.get(plan_name, 0) <= latest_run_id


def _record_task_state(
    task_statuses: dict[tuple[str, str], dict[str, str]],
    task_phases: dict[tuple[str, str], str],
    key: tuple[str, str],
    state: TaskState,
) -> None:
    plan_name, task_slug = key
    task_statuses[key] = {
        "slug": task_slug,
        "state": state,
        "plan_name": plan_name,
    }
    if state in TERMINAL_STATES:
        task_phases.pop(key, None)


def _record_task_phase(
    task_phases: dict[tuple[str, str], str],
    key: tuple[str, str],
    event: dict,
) -> None:
    phase = phase_from_event(event)
    if phase is not None:
        task_phases[key] = phase
    elif event.get("event") == "settlement_phase_completed":
        task_phases.pop(key, None)


def _apply_task_phases(
    task_statuses: dict[tuple[str, str], dict[str, str]],
    task_phases: dict[tuple[str, str], str],
) -> None:
    for key, phase in task_phases.items():
        if key not in task_statuses:
            continue
        if phase.startswith("settling:"):
            task_statuses[key]["state"] = "settling"
            task_statuses[key]["phase"] = phase.split(":", 1)[1]
        else:
            task_statuses[key]["phase"] = phase


def _mark_stale_historical_tasks(
    task_statuses: dict[tuple[str, str], dict[str, str]],
    task_phases: dict[tuple[str, str], str],
    task_event_ids: dict[tuple[str, str], int],
    latest_boundary_ids: dict[str, int],
) -> None:
    for key, task in task_statuses.items():
        if task.get("state") not in LIVE_STATES:
            continue

        plan_name, _task_slug = key
        if latest_boundary_ids.get(plan_name, 0) <= task_event_ids.get(key, 0):
            continue

        task["state"] = "stale"
        task.pop("phase", None)
        task_phases.pop(key, None)


def _task_event_in_scope(
    event: dict,
    *,
    latest_run_only: bool,
    latest_run_ids: dict[str, int],
    latest_completed_ids: dict[str, int],
) -> bool:
    return not latest_run_only or _event_is_in_latest_run_window(
        event,
        latest_run_ids=latest_run_ids,
        latest_completed_ids=latest_completed_ids,
    )


def _record_task_event(
    event: dict,
    key: tuple[str, str],
    task_statuses: dict[tuple[str, str], dict[str, str]],
    task_phases: dict[tuple[str, str], str],
    task_event_ids: dict[tuple[str, str], int],
) -> None:
    task_event_ids[key] = _event_id(event)
    state = state_from_event(event)
    if state is not None:
        _record_task_state(task_statuses, task_phases, key, state)
    _record_task_phase(task_phases, key, event)


def _collect_task_statuses(
    events: list[dict],
    *,
    latest_run_only: bool,
    latest_run_ids: dict[str, int],
    latest_completed_ids: dict[str, int],
) -> tuple[
    dict[tuple[str, str], dict[str, str]],
    dict[tuple[str, str], str],
    dict[tuple[str, str], int],
]:
    task_statuses: dict[tuple[str, str], dict[str, str]] = {}
    task_phases: dict[tuple[str, str], str] = {}
    task_event_ids: dict[tuple[str, str], int] = {}
    for event in events:
        key = _task_key(event)
        if key is None:
            continue
        if not _task_event_in_scope(
            event,
            latest_run_only=latest_run_only,
            latest_run_ids=latest_run_ids,
            latest_completed_ids=latest_completed_ids,
        ):
            continue
        _record_task_event(event, key, task_statuses, task_phases, task_event_ids)
    return task_statuses, task_phases, task_event_ids


def _finalize_task_statuses(
    events: list[dict],
    task_statuses: dict[tuple[str, str], dict[str, str]],
    task_phases: dict[tuple[str, str], str],
    task_event_ids: dict[tuple[str, str], int],
    *,
    latest_run_only: bool,
) -> None:
    _apply_task_phases(task_statuses, task_phases)
    if not latest_run_only:
        _mark_stale_historical_tasks(
            task_statuses,
            task_phases,
            task_event_ids,
            latest_plan_boundary_ids(events),
        )


def tasks_from_events(project_root: str, *, latest_run_only: bool) -> list[dict[str, str]]:
    """Build task snapshots from lifecycle events instead of mutable task rows.

    Tracks both state and settlement phase. A task with an open settlement phase
    remains visible as 'settling' until merge_completed or task_merge_failed.
    """
    events = read_events(project_root)
    if not events:
        return []

    latest_run_ids = latest_run_start_ids(events) if latest_run_only else {}
    latest_completed_ids = latest_run_completed_ids(events) if latest_run_only else {}
    task_statuses, task_phases, task_event_ids = _collect_task_statuses(
        events,
        latest_run_only=latest_run_only,
        latest_run_ids=latest_run_ids,
        latest_completed_ids=latest_completed_ids,
    )
    _finalize_task_statuses(
        events,
        task_statuses,
        task_phases,
        task_event_ids,
        latest_run_only=latest_run_only,
    )

    return sorted(
        task_statuses.values(),
        key=lambda task: (task.get("plan_name", ""), task["slug"]),
    )


def live_plan_names(project_root: str) -> set[str]:
    """Return plan names that currently have live tasks in the latest run window."""
    return {
        str(task["plan_name"])
        for task in tasks_from_events(project_root, latest_run_only=True)
        if task.get("plan_name") and task.get("state") in LIVE_STATES
    }
