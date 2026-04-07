"""Schema definitions for persistence layer.

TaskState is imported from types.py (single source of truth).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from dgov.persistence.sql import (
    _CREATE_EVENTS_TABLE_SQL,
    _CREATE_SLUG_HISTORY_TABLE_SQL,
    _CREATE_TABLE_SQL,
)
from dgov.types import TaskState

# -- Constants --

STATE_DIR = ".dgov"
_STATE_FILE = "state.db"
_SCHEMA_VERSION = 7  # Added plan_name to events

TASK_STATES = frozenset(TaskState)

# Transition table: enforced in update_task_state
# Review is mandatory before merge — no direct done->merged or active->merged.
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {
            TaskState.ACTIVE,
            TaskState.FAILED,
            TaskState.SKIPPED,
            TaskState.CLOSED,
        }
    ),
    TaskState.ACTIVE: frozenset(
        {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.ABANDONED,
            TaskState.TIMED_OUT,
            TaskState.CLOSED,
        }
    ),
    TaskState.DONE: frozenset(
        {
            TaskState.REVIEWING,
            TaskState.CLOSED,
        }
    ),
    TaskState.REVIEWING: frozenset(
        {
            TaskState.REVIEWED_PASS,
            TaskState.REVIEWED_FAIL,
            TaskState.CLOSED,
        }
    ),
    TaskState.REVIEWED_FAIL: frozenset({TaskState.FAILED, TaskState.CLOSED}),
    TaskState.REVIEWED_PASS: frozenset({TaskState.MERGING, TaskState.FAILED, TaskState.CLOSED}),
    TaskState.MERGING: frozenset({TaskState.MERGED, TaskState.FAILED, TaskState.CLOSED}),
    TaskState.FAILED: frozenset({TaskState.PENDING, TaskState.CLOSED}),
    TaskState.MERGED: frozenset({TaskState.CLOSED}),
    TaskState.TIMED_OUT: frozenset({TaskState.CLOSED}),
    TaskState.CLOSED: frozenset(),
    TaskState.ABANDONED: frozenset({TaskState.CLOSED}),
    TaskState.SKIPPED: frozenset({TaskState.CLOSED}),
}


class IllegalTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: str | TaskState, target: str | TaskState, slug: str) -> None:
        self.current = current
        self.target = target
        self.slug = slug
        super().__init__(f"Illegal state transition for '{slug}': {current} -> {target}")


# -- WorkerTask Dataclass --


@dataclass(frozen=True, slots=True)
class WorkerTask:
    """Represents a worker task record — immutable, strictly validated."""

    slug: str
    prompt: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    task_id: str | None = None  # worker instance ID; None for headless
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str | None = None
    role: str = "worker"
    state: TaskState = TaskState.ACTIVE
    plan_name: str | None = None
    file_claims: tuple[str, ...] = ()
    commit_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, TaskState):
            raise TypeError(
                f"state must be TaskState, got {type(self.state).__name__}: {self.state!r}"
            )
        if not self.slug:
            raise ValueError("slug must be non-empty")
        if not self.prompt:
            raise ValueError("prompt must be non-empty")


_TASK_COLUMNS = frozenset(
    {
        "slug",
        "prompt",
        "task_id",
        "agent",
        "project_root",
        "worktree_path",
        "branch_name",
        "created_at",
        "owns_worktree",
        "base_sha",
        "provenance",
        "role",
        "state",
    }
)

_TASK_TYPED_COLS = frozenset(
    {
        "plan_name",
        "file_claims",
        "commit_message",
    }
)

# -- Path Helpers --


def state_path(session_root: str) -> Path:
    """Return the path to the state database file."""
    return Path(session_root) / STATE_DIR / _STATE_FILE


# -- Event Constants --

# Only events emitted by the Lacustrine kernel + runner
VALID_EVENTS = frozenset(
    {
        # Runner lifecycle
        "task_created",
        "task_done",
        "task_failed",
        "task_closed",
        "shutdown_requested",
        # DAG lifecycle
        "dag_task_dispatched",
        "dag_completed",
        "dag_failed",
        # Review
        "review_pass",
        "review_fail",
        # Merge
        "merge_completed",
        "task_merge_failed",
        # Worker subprocess
        "worker_log",
        "worker_done",
        "worker_error",
    }
)

_EVENT_TYPED_COLS = frozenset(
    {
        "task_slug",
        "plan_name",
        "commit_count",
        "error",
        "reason",
        "merge_sha",
        "branch",
        "new_slug",
        "target_agent",
        "message",
    }
)


__all__ = [
    "STATE_DIR",
    "_STATE_FILE",
    "_SCHEMA_VERSION",
    "TASK_STATES",
    "VALID_TRANSITIONS",
    "IllegalTransitionError",
    "WorkerTask",
    "_TASK_COLUMNS",
    "_TASK_TYPED_COLS",
    "_CREATE_TABLE_SQL",
    "_CREATE_EVENTS_TABLE_SQL",
    "_CREATE_SLUG_HISTORY_TABLE_SQL",
    "state_path",
    "VALID_EVENTS",
    "_EVENT_TYPED_COLS",
]
