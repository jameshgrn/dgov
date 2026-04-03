"""Pane state operations (deprecated).

This module is a backwards-compatible wrapper around tasks.py.
All functions here delegate to the task-oriented versions.

Migrate to: from dgov.persistence import tasks
"""

from __future__ import annotations

from dgov.persistence import tasks
from dgov.persistence.schema import (
    CompletionTransitionResult,
    IllegalTransitionError,
    PaneState,  # deprecated alias for TaskState
    WorkerPane,  # deprecated alias for WorkerTask
)

# Re-export all task functions with pane names for backwards compatibility
add_pane = tasks.add_task
remove_pane = tasks.remove_task
get_pane = tasks.get_task
get_panes = tasks.get_tasks
all_panes = tasks.all_tasks
update_pane_state = tasks.update_task_state
settle_completion_state = tasks.settle_completion_state
settle_closed = tasks.settle_closed
settled_panes = tasks.settled_tasks
active_panes = tasks.active_tasks
count_active = tasks.count_active
update_file_claims = tasks.update_file_claims
get_slug_history = tasks.get_slug_history
replace_all_panes = tasks.replace_all_tasks
set_pane_metadata = tasks.set_task_metadata

__all__ = [
    "add_pane",
    "remove_pane",
    "get_pane",
    "get_panes",
    "all_panes",
    "update_pane_state",
    "settle_completion_state",
    "settle_closed",
    "settled_panes",
    "active_panes",
    "count_active",
    "update_file_claims",
    "get_slug_history",
    "replace_all_panes",
    "set_pane_metadata",
    "WorkerPane",
    "PaneState",
    "CompletionTransitionResult",
    "IllegalTransitionError",
]
