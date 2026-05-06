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


def state_from_event(event: dict) -> str | None:
    """Map a lifecycle event to the task state it establishes."""
    event_name = event.get("event")
    if event_name == "dag_task_dispatched":
        return "active"
    if event_name == "task_done":
        return "done"
    if event_name == "task_abandoned":
        return "abandoned"
    if event_name == "task_timed_out":
        return "timed_out"
    if event_name == "task_failed":
        error = str(event.get("error", "")).lower()
        if "timeout" in error:
            return "timed_out"
        return "failed"
    if event_name == "review_pass":
        return "reviewed_pass"
    if event_name == "review_fail":
        return "reviewed_fail"
    if event_name == "merge_completed":
        return "merged"
    if event_name == "task_merge_failed":
        return "failed"
    if event_name == "task_closed":
        return "closed"
    if event_name == "dag_task_governor_resumed":
        action = event.get("action")
        if action == "retry":
            return "pending"
        if action == "skip":
            return "skipped"
        if action == "fail":
            return "failed"
    # Settlement phase events don't directly change state but are tracked separately
    return None


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
        latest[str(plan_name)] = max(latest.get(str(plan_name), 0), int(event.get("id", 0)))
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
        latest[str(plan_name)] = max(latest.get(str(plan_name), 0), int(event.get("id", 0)))
    return latest


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
        task_slug = event.get("task_slug")
        if not task_slug:
            continue
        plan_name = str(event.get("plan_name") or "")
        if latest_run_only:
            latest_run_id = latest_run_ids.get(plan_name, 0)
            if int(event.get("id", 0)) <= latest_run_id:
                continue
            if latest_completed_ids.get(plan_name, 0) > latest_run_id:
                continue

        # Track state transitions
        state = state_from_event(event)
        if state is not None:
            task_statuses[(plan_name, str(task_slug))] = {
                "slug": str(task_slug),
                "state": state,
                "plan_name": plan_name,
            }
            # Clear phase when reaching terminal state
            if state in TERMINAL_STATES:
                task_phases.pop((plan_name, str(task_slug)), None)

        # Track settlement phases
        phase = phase_from_event(event)
        if phase is not None:
            task_phases[(plan_name, str(task_slug))] = phase
        elif event.get("event") == "settlement_phase_completed":
            # Clear phase when settlement phase completes
            # The final state will be set by merge_completed/task_merge_failed
            task_phases.pop((plan_name, str(task_slug)), None)

    # Merge phase info into task statuses
    for key, phase in task_phases.items():
        if key in task_statuses:
            # If task has an open settlement phase, mark it as settling
            if phase.startswith("settling:"):
                task_statuses[key]["state"] = "settling"
                task_statuses[key]["phase"] = phase.split(":", 1)[1]
            else:
                task_statuses[key]["phase"] = phase

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
