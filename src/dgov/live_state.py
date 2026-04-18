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
    "reviewed_pass",
    "reviewed_fail",
    "merging",
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


def tasks_from_events(project_root: str, *, latest_run_only: bool) -> list[dict[str, str]]:
    """Build task snapshots from lifecycle events instead of mutable task rows."""
    events = read_events(project_root)
    if not events:
        return []

    latest_run_ids = latest_run_start_ids(events) if latest_run_only else {}
    task_statuses: dict[tuple[str, str], dict[str, str]] = {}

    for event in events:
        task_slug = event.get("task_slug")
        if not task_slug:
            continue
        plan_name = str(event.get("plan_name") or "")
        if latest_run_only and int(event.get("id", 0)) <= latest_run_ids.get(plan_name, 0):
            continue
        state = state_from_event(event)
        if state is None:
            continue
        task_statuses[(plan_name, str(task_slug))] = {
            "slug": str(task_slug),
            "state": state,
            "plan_name": plan_name,
        }

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
