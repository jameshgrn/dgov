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
from dgov.persistence.schema import (
    STATE_DIR,
    PaneState,
    WorkerPane,
)
from dgov.persistence.state_ops import (
    add_pane,
    all_panes,
    get_pane,
    get_panes,
    remove_pane,
    replace_all_panes,
    set_pane_metadata,
    update_pane_state,
)

__all__ = [
    "STATE_DIR",
    "PaneState",
    "WorkerPane",
    "_get_db",
    "add_pane",
    "all_panes",
    "clear_connection_cache",
    "emit_event",
    "get_pane",
    "get_panes",
    "read_events",
    "wait_for_events",
    "latest_event_id",
    "remove_pane",
    "replace_all_panes",
    "update_pane_state",
    "set_pane_metadata",
]
