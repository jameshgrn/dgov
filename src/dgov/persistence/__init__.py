"""Persistence layer for dgov.

Event log data is the authority for lifecycle state. Runtime artifact rows are
best-effort bookkeeping for worktrees, branches, and related execution crumbs.
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
    reset_task_state,
)
from dgov.persistence.ledger import (
    add_ledger_entry,
    list_ledger_entries,
    resolve_ledger_entry,
)
from dgov.persistence.runtime_artifacts import (
    get_runtime_artifact,
    get_runtime_artifacts,
    get_slug_history,
    list_runtime_artifacts,
    prune_runtime_artifact_history,
    record_runtime_artifact,
    remove_runtime_artifact,
    replace_runtime_artifacts,
    set_runtime_artifact_metadata,
    update_runtime_artifact_state,
)
from dgov.persistence.schema import (
    TaskState,
    WorkerTask,
    state_path,
)

__all__ = [
    "TaskState",
    "WorkerTask",
    "_get_db",
    "add_ledger_entry",
    "clear_connection_cache",
    "emit_event",
    "get_runtime_artifact",
    "get_runtime_artifacts",
    "get_slug_history",
    "latest_event_id",
    "list_ledger_entries",
    "list_runtime_artifacts",
    "prune_runtime_artifact_history",
    "read_events",
    "record_runtime_artifact",
    "remove_runtime_artifact",
    "replace_runtime_artifacts",
    "reset_plan_state",
    "reset_task_state",
    "resolve_ledger_entry",
    "set_runtime_artifact_metadata",
    "state_path",
    "update_runtime_artifact_state",
]
