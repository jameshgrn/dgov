"""Persistence layer for dgov.

State file management for task records and event log via SQLite.
SQL tables retain 'pane' names for backwards compatibility.
"""

from __future__ import annotations

from dgov.persistence.connection import (
    _get_db,
    clear_connection_cache,
)
from dgov.persistence.events import (
    emit_event,
    latest_event_id,
    read_events,
    wait_for_events,
)
from dgov.persistence.panes import (
    get_slug_history,
    remove_pane,
    settle_completion_state,
)
from dgov.persistence.schema import (
    TaskState,
    WorkerTask,
    state_path,
)
from dgov.persistence.tasks import (
    add_task,
    all_tasks,
    get_task,
    get_tasks,
    replace_all_tasks,
    set_task_metadata,
    update_task_state,
)

__all__ = [
    # Types
    "TaskState",
    "WorkerTask",
    # Task operations
    "add_task",
    "all_tasks",
    "get_task",
    "get_tasks",
    "replace_all_tasks",
    "set_task_metadata",
    "update_task_state",
    # Utilities
    "_get_db",
    "clear_connection_cache",
    "emit_event",
    "get_slug_history",
    "latest_event_id",
    "read_events",
    "remove_pane",
    "settle_completion_state",
    "state_path",
    "wait_for_events",
]
