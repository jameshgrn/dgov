"""Schema definitions for persistence layer.

PaneState is imported from types.py (single source of truth).
Only tables used by the Lacustrine kernel are referenced here.
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
from dgov.types import PaneState

# -- Constants --

STATE_DIR = ".dgov"
_STATE_FILE = "state.db"
_SCHEMA_VERSION = 4  # Bumped: stripped monitor/tiered columns

# Re-export PaneState from types.py (single source of truth)
PANE_STATES = frozenset(PaneState)

# Transition table: enforced in update_pane_state
# Review is mandatory before merge — no direct done->merged or active->merged.
VALID_TRANSITIONS: dict[PaneState, frozenset[PaneState]] = {
    PaneState.ACTIVE: frozenset(
        {
            PaneState.DONE,
            PaneState.FAILED,
            PaneState.ABANDONED,
            PaneState.TIMED_OUT,
            PaneState.CLOSED,
        }
    ),
    PaneState.DONE: frozenset(
        {
            PaneState.REVIEWED_PASS,
            PaneState.REVIEWED_FAIL,
            PaneState.CLOSED,
        }
    ),
    PaneState.FAILED: frozenset({PaneState.CLOSED}),
    PaneState.REVIEWED_PASS: frozenset({PaneState.MERGED, PaneState.FAILED, PaneState.CLOSED}),
    PaneState.REVIEWED_FAIL: frozenset({PaneState.CLOSED}),
    PaneState.MERGED: frozenset({PaneState.CLOSED}),
    PaneState.TIMED_OUT: frozenset({PaneState.CLOSED}),
    PaneState.SUPERSEDED: frozenset({PaneState.CLOSED}),
    PaneState.CLOSED: frozenset(),
    PaneState.ABANDONED: frozenset({PaneState.CLOSED}),
}

_COMPLETION_TARGET_STATES = frozenset(
    {PaneState.DONE, PaneState.FAILED, PaneState.ABANDONED, PaneState.TIMED_OUT}
)
_SETTLED_PANE_STATES = PANE_STATES - {PaneState.ACTIVE}


class IllegalTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: str | PaneState, target: str | PaneState, slug: str):
        self.current = current
        self.target = target
        self.slug = slug
        super().__init__(f"Illegal state transition for '{slug}': {current} -> {target}")


@dataclass(frozen=True)
class CompletionTransitionResult:
    """Result of a completion state transition attempt."""

    changed: bool
    state: PaneState = PaneState.ACTIVE


# -- Provenance --


@dataclass(frozen=True, slots=True)
class ProvenanceOriginal:
    """Original pane — not derived from another."""

    kind: Literal["original"] = "original"


@dataclass(frozen=True, slots=True)
class ProvenanceRetry:
    """Retry of a failed pane."""

    original_slug: str
    attempt: int = 1
    kind: Literal["retry"] = "retry"


PaneProvenance = ProvenanceOriginal | ProvenanceRetry


# -- WorkerPane Dataclass --


@dataclass(frozen=True, slots=True)
class WorkerPane:
    """Represents a worker pane record — immutable, strictly validated."""

    slug: str
    prompt: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    pane_id: str | None = None
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str | None = None
    provenance: PaneProvenance = field(default_factory=ProvenanceOriginal)
    role: str = "worker"
    state: PaneState = PaneState.ACTIVE
    file_claims: tuple[str, ...] = ()
    commit_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PaneState):
            raise TypeError(
                f"state must be PaneState, got {type(self.state).__name__}: {self.state!r}"
            )
        if not self.slug:
            raise ValueError("slug must be non-empty")
        if not self.prompt:
            raise ValueError("prompt must be non-empty")


_PANE_COLUMNS = frozenset(
    {
        "slug",
        "prompt",
        "pane_id",
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

_PANE_TYPED_COLS = frozenset(
    {
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
    "PaneState",
    "PANE_STATES",
    "VALID_TRANSITIONS",
    "IllegalTransitionError",
    "CompletionTransitionResult",
    "WorkerPane",
    "replace",
    "_PANE_COLUMNS",
    "_PANE_TYPED_COLS",
    "_CREATE_TABLE_SQL",
    "_CREATE_EVENTS_TABLE_SQL",
    "_CREATE_SLUG_HISTORY_TABLE_SQL",
    "state_path",
    "VALID_EVENTS",
    "_EVENT_TYPED_COLS",
    "_COMPLETION_TARGET_STATES",
    "_SETTLED_PANE_STATES",
]
