"""Persistence layer for dgov.

State file management for task records and event log via SQLite.
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
    reset_plan_state,
)
from dgov.persistence.ledger import (
    add_ledger_entry,
    list_ledger_entries,
    resolve_ledger_entry,
)
from dgov.persistence.schema import (
    TaskState,
    WorkerTask,
    state_path,
)
from dgov.persistence.tasks import (
    add_task,
    all_tasks,
    cleanup_zombies,
    get_slug_history,
    get_task,
    get_tasks,
    remove_task,
    replace_all_tasks,
    set_task_metadata,
    update_task_state,
)

__all__ = [
    # Types
    "TaskState",
    "WorkerTask",
    # Utilities
    "_get_db",
    # Ledger
    "add_ledger_entry",
    # Task operations
    "add_task",
    "all_tasks",
    "cleanup_zombies",
    "clear_connection_cache",
    "emit_event",
    "get_slug_history",
    "get_task",
    "get_tasks",
    "latest_event_id",
    "list_ledger_entries",
    "read_events",
    "remove_task",
    "replace_all_tasks",
    "reset_plan_state",
    "resolve_ledger_entry",
    "set_task_metadata",
    "state_path",
    "update_task_state",
]
