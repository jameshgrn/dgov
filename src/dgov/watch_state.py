"""Watch state model — mode/cursor tracking for dgov watch.

No CLI, Rich, or Click imports. Pure model and persistence access only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dgov.live_state import latest_run_start_ids, live_plan_names
from dgov.persistence import latest_event_id, read_events

WatchMode = Literal["follow", "pinned", "all"]

_KNOWN_FIELDS = frozenset({"id", "ts", "event", "pane", "plan_name", "task_slug"})


@dataclass(frozen=True)
class PlanSwitchUpdate:
    """Emitted by follow mode when the tracked plan changes."""

    from_plan: str | None
    to_plan: str | None


@dataclass(frozen=True)
class EventRowUpdate:
    """A single normalized event row ready for rendering."""

    id: int
    ts: str
    event: str
    pane: str
    plan_name: str | None
    task_slug: str | None
    payload: dict


def _to_event_row(row: dict) -> EventRowUpdate:
    return EventRowUpdate(
        id=int(row.get("id", 0)),
        ts=str(row.get("ts", "")),
        event=str(row.get("event", "")),
        pane=str(row.get("pane", "")),
        plan_name=row.get("plan_name") or None,
        task_slug=row.get("task_slug") or None,
        payload={k: v for k, v in row.items() if k not in _KNOWN_FIELDS},
    )


def _run_start_cursor(project_root: str, plan_name: str) -> int:
    """Return the latest run_start event id for a plan, or 0 if none exists."""
    events = read_events(project_root, plan_name=plan_name)
    return latest_run_start_ids(events).get(plan_name, 0)


@dataclass
class WatchSession:
    """Stateful cursor and plan selection for the dgov watch polling loop.

    Call initialize() once before the first poll().
    """

    project_root: str
    mode: WatchMode
    pinned_plan: str | None = None

    _cursor: int = field(default=0, init=False, repr=False)
    _active_plan: str | None = field(default=None, init=False, repr=False)

    @property
    def active_plan(self) -> str | None:
        return self._active_plan

    @property
    def cursor(self) -> int:
        return self._cursor

    def initialize(self) -> None:
        """Set initial cursor and active plan. Must be called before poll()."""
        if self.mode == "all":
            self._cursor = 0
            self._active_plan = None
            return

        if self.mode == "pinned":
            self._active_plan = self.pinned_plan
            self._cursor = _run_start_cursor(self.project_root, self.pinned_plan or "")
            return

        # follow mode: pick up a live plan if exactly one exists
        live = live_plan_names(self.project_root)
        if len(live) == 1:
            plan = next(iter(live))
            self._active_plan = plan
            self._cursor = _run_start_cursor(self.project_root, plan)
        else:
            self._active_plan = None
            self._cursor = latest_event_id(self.project_root)

    def poll(self) -> list[PlanSwitchUpdate | EventRowUpdate]:
        """Return new updates since the last poll. Advances the internal cursor."""
        if self.mode == "all":
            return self._poll_all()
        if self.mode == "pinned":
            return self._poll_pinned()
        return self._poll_follow()

    def _poll_all(self) -> list[PlanSwitchUpdate | EventRowUpdate]:
        rows = read_events(self.project_root, after_id=self._cursor)
        updates: list[PlanSwitchUpdate | EventRowUpdate] = []
        for row in rows:
            self._cursor = max(self._cursor, int(row.get("id", 0)))
            updates.append(_to_event_row(row))
        return updates

    def _poll_pinned(self) -> list[PlanSwitchUpdate | EventRowUpdate]:
        rows = read_events(self.project_root, after_id=self._cursor, plan_name=self._active_plan)
        updates: list[PlanSwitchUpdate | EventRowUpdate] = []
        for row in rows:
            self._cursor = max(self._cursor, int(row.get("id", 0)))
            updates.append(_to_event_row(row))
        return updates

    def _poll_follow(self) -> list[PlanSwitchUpdate | EventRowUpdate]:
        updates: list[PlanSwitchUpdate | EventRowUpdate] = []
        live = live_plan_names(self.project_root)

        if self._active_plan is None:
            if len(live) == 1:
                new_plan = next(iter(live))
                self._active_plan = new_plan
                self._cursor = _run_start_cursor(self.project_root, new_plan)
                updates.append(PlanSwitchUpdate(from_plan=None, to_plan=new_plan))
            else:
                # Idle: advance cursor so prior events are never replayed once a plan starts
                self._cursor = latest_event_id(self.project_root)
                return updates

        rows = read_events(self.project_root, after_id=self._cursor, plan_name=self._active_plan)
        for row in rows:
            self._cursor = max(self._cursor, int(row.get("id", 0)))
            updates.append(_to_event_row(row))

        # Prior plan completed and a new single live plan appeared: switch
        if self._active_plan not in live and len(live) == 1:
            new_plan = next(iter(live))
            updates.append(PlanSwitchUpdate(from_plan=self._active_plan, to_plan=new_plan))
            self._active_plan = new_plan
            self._cursor = _run_start_cursor(self.project_root, new_plan)

        return updates
