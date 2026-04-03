"""Schema definitions for persistence layer.

TaskState is imported from types.py (single source of truth).
SQL tables retain 'pane' names for backwards compatibility.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dgov.persistence.sql import (
    _CREATE_EVENTS_TABLE_SQL,
    _CREATE_SLUG_HISTORY_TABLE_SQL,
    _CREATE_TABLE_SQL,
)
from dgov.types import TaskState

# -- Constants --

STATE_DIR = ".dgov"
_STATE_FILE = "state.db"
_SCHEMA_VERSION = 4  # Bumped: stripped monitor/tiered columns

# Re-export PaneState from types.py (single source of truth)
TASK_STATES = frozenset(TaskState)

# Deprecated aliases
PANE_STATES = TASK_STATES
PaneState = TaskState

# Transition table: enforced in update_task_state
# Review is mandatory before merge — no direct done->merged or active->merged.
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
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
            TaskState.REVIEWED_PASS,
            TaskState.REVIEWED_FAIL,
            TaskState.CLOSED,
        }
    ),
    TaskState.FAILED: frozenset({TaskState.CLOSED}),
    TaskState.REVIEWED_PASS: frozenset({TaskState.MERGED, TaskState.FAILED, TaskState.CLOSED}),
    TaskState.REVIEWED_FAIL: frozenset({TaskState.CLOSED}),
    TaskState.MERGED: frozenset({TaskState.CLOSED}),
    TaskState.TIMED_OUT: frozenset({TaskState.CLOSED}),
    TaskState.SUPERSEDED: frozenset({TaskState.CLOSED}),
    TaskState.CLOSED: frozenset(),
    TaskState.ABANDONED: frozenset({TaskState.CLOSED}),
}

_COMPLETION_TARGET_STATES = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.ABANDONED, TaskState.TIMED_OUT}
)
_SETTLED_TASK_STATES = TASK_STATES - {TaskState.ACTIVE}

# Deprecated aliases
_SETTLED_PANE_STATES = _SETTLED_TASK_STATES


class IllegalTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: str | TaskState, target: str | TaskState, slug: str):
        self.current = current
        self.target = target
        self.slug = slug
        super().__init__(f"Illegal state transition for '{slug}': {current} -> {target}")


@dataclass(frozen=True)
class CompletionTransitionResult:
    """Result of a completion state transition attempt."""

    changed: bool
    state: TaskState = TaskState.ACTIVE


# -- Provenance --


@dataclass(frozen=True, slots=True)
class ProvenanceOriginal:
    """Original task — not derived from another."""

    kind: Literal["original"] = "original"


@dataclass(frozen=True, slots=True)
class ProvenanceRetry:
    """Retry of a failed task."""

    original_slug: str
    attempt: int = 1
    kind: Literal["retry"] = "retry"


TaskProvenance = ProvenanceOriginal | ProvenanceRetry

# Deprecated alias
PaneProvenance = TaskProvenance


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
    task_id: str | None = None  # tmux pane_id when in pane mode; None for headless
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str | None = None
    provenance: TaskProvenance = field(default_factory=ProvenanceOriginal)
    role: str = "worker"
    state: TaskState = TaskState.ACTIVE
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


# Deprecated alias
WorkerPane = WorkerTask


_TASK_COLUMNS = frozenset(
    {
        "slug",
        "prompt",
        "task_id",  # maps to DB column 'pane_id' for backwards compatibility
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
        "file_claims",
        "commit_message",
    }
)

# Deprecated aliases
_PANE_COLUMNS = _TASK_COLUMNS
_PANE_TYPED_COLS = _TASK_TYPED_COLS

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
        # Deprecated aliases (deprecated, use task_* events)
        "pane_created",
        "pane_done",
        "pane_failed",
        "pane_closed",
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
        # Deprecated alias
        "pane_merge_failed",
        # Worker subprocess
        "worker_log",
        "worker_done",
        "worker_error",
    }
)

_EVENT_TYPED_COLS = frozenset(
    {
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
    # Types imported from types.py (not re-exported here)
    # "TaskState",
    # "TASK_STATES",
    "VALID_TRANSITIONS",
    "IllegalTransitionError",
    "CompletionTransitionResult",
    "WorkerTask",
    "TaskProvenance",
    "replace",
    "_TASK_COLUMNS",
    "_TASK_TYPED_COLS",
    "_CREATE_TABLE_SQL",
    "_CREATE_EVENTS_TABLE_SQL",
    "_CREATE_SLUG_HISTORY_TABLE_SQL",
    "state_path",
    "VALID_EVENTS",
    "_EVENT_TYPED_COLS",
    "_COMPLETION_TARGET_STATES",
    "_SETTLED_TASK_STATES",
    # Deprecated aliases
    "PaneState",
    "PANE_STATES",
    "WorkerPane",
    "PaneProvenance",
    "_PANE_COLUMNS",
    "_PANE_TYPED_COLS",
    "_SETTLED_PANE_STATES",
]
