"""Persistence layer for dgov.

State file management for pane records and event log via SQLite.
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
    add_pane,
    all_panes,
    get_pane,
    get_panes,
    get_slug_history,
    remove_pane,
    replace_all_panes,
    set_pane_metadata,
    settle_completion_state,
    update_pane_state,
)
from dgov.persistence.schema import (
    PaneState,
    WorkerPane,
    state_path,
)

__all__ = [
    "PaneState",
    "WorkerPane",
    "_get_db",
    "add_pane",
    "all_panes",
    "clear_connection_cache",
    "emit_event",
    "get_pane",
    "get_panes",
    "get_slug_history",
    "latest_event_id",
    "read_events",
    "remove_pane",
    "replace_all_panes",
    "set_pane_metadata",
    "settle_completion_state",
    "state_path",
    "update_pane_state",
    "wait_for_events",
]
