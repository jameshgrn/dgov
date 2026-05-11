"""Event-derived live state helpers.

These helpers treat the event log as the source of truth for current governor
state. Mutable task rows are persistence/cache, not authority.
"""

from __future__ import annotations

from dgov.persistence import read_events

LIVE_STATES = frozenset({
    "pending",
    "active",
    "done",
    "reviewing",
    "merging",
    "settling",
})

# Settlement phases that indicate active settlement work
SETTLEMENT_PHASES = frozenset({
    "integration",
    "semantic_gate",
    "merge",
})

# Terminal states that end settlement visibility
TERMINAL_STATES = frozenset({
    "merged",
    "failed",
    "closed",
    "abandoned",
    "timed_out",
    "skipped",
})

_EVENT_STATE_MAP = {
    "dag_task_dispatched": "active",
    "task_done": "done",
    "task_abandoned": "abandoned",
    "task_timed_out": "timed_out",
    "review_pass": "reviewed_pass",
    "review_fail": "reviewed_fail",
    "merge_completed": "merged",
    "task_merge_failed": "failed",
    "task_closed": "closed",
}

_GOVERNOR_RESUME_STATE_MAP = {
    "retry": "pending",
    "skip": "skipped",
    "fail": "failed",
}


def _task_key(event: dict) -> tuple[str, str] | None:
    task_slug = event.get("task_slug")
    if not task_slug:
        return None
    return str(event.get("plan_name") or ""), str(task_slug)


def _event_id(event: dict) -> int:
    return int(event.get("id", 0))


def state_from_event(event: dict) -> str | None:
    """Map a lifecycle event to the task state it establishes."""
    event_name = event.get("event")
    if event_name == "task_failed":
        error = str(event.get("error", "")).lower()
        return "timed_out" if "timeout" in error else "failed"
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
    state: str,
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
    task_statuses: dict[tuple[str, str], dict[str, str]] = {}
    task_phases: dict[tuple[str, str], str] = {}

    for event in events:
        key = _task_key(event)
        if key is None:
            continue
        if latest_run_only and not _event_is_in_latest_run_window(
            event,
            latest_run_ids=latest_run_ids,
            latest_completed_ids=latest_completed_ids,
        ):
            continue

        state = state_from_event(event)
        if state is not None:
            _record_task_state(task_statuses, task_phases, key, state)

        _record_task_phase(task_phases, key, event)

    _apply_task_phases(task_statuses, task_phases)

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
