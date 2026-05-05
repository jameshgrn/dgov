"""Schema definitions for persistence layer.

TaskState is imported from types.py (single source of truth).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dgov.persistence.sql import (
    _CREATE_EVENTS_TABLE_SQL,
    _CREATE_LEDGER_TABLE_SQL,
    _CREATE_SLUG_HISTORY_TABLE_SQL,
    _CREATE_TABLE_SQL,
)
from dgov.types import TaskState

# -- Constants --

STATE_DIR = ".dgov"
_STATE_FILE = "state.db"
_SCHEMA_VERSION = 8  # Added ledger table

TASK_STATES = frozenset(TaskState)

# Transition table: enforced in update_task_state
# Review is mandatory before merge — no direct done->merged or active->merged.
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({
        TaskState.ACTIVE,
        TaskState.FAILED,
        TaskState.SKIPPED,
        TaskState.CLOSED,
    }),
    TaskState.ACTIVE: frozenset({
        TaskState.DONE,
        TaskState.FAILED,
        TaskState.ABANDONED,
        TaskState.TIMED_OUT,
        TaskState.CLOSED,
    }),
    TaskState.DONE: frozenset({
        TaskState.REVIEWING,
        TaskState.CLOSED,
    }),
    TaskState.REVIEWING: frozenset({
        TaskState.REVIEWED_PASS,
        TaskState.REVIEWED_FAIL,
        TaskState.CLOSED,
    }),
    TaskState.REVIEWED_FAIL: frozenset({TaskState.FAILED, TaskState.CLOSED}),
    TaskState.REVIEWED_PASS: frozenset({TaskState.MERGING, TaskState.FAILED, TaskState.CLOSED}),
    TaskState.MERGING: frozenset({TaskState.MERGED, TaskState.FAILED, TaskState.CLOSED}),
    TaskState.FAILED: frozenset({TaskState.PENDING, TaskState.CLOSED}),
    TaskState.MERGED: frozenset({TaskState.CLOSED}),
    TaskState.TIMED_OUT: frozenset({TaskState.PENDING, TaskState.CLOSED}),
    TaskState.CLOSED: frozenset(),
    TaskState.ABANDONED: frozenset({TaskState.PENDING, TaskState.CLOSED}),
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
    """Runtime artifact row for a worker attempt.

    This row tracks operational metadata like worktree and branch identity.
    Lifecycle truth comes from events, not from this cached snapshot.
    """

    slug: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    prompt: str = ""
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


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Represents an entry in the operational ledger."""

    id: int | None
    category: str
    content: str
    status: Literal["open", "resolved"] = "open"
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    affected_paths: tuple[str, ...] = ()
    affected_tags: tuple[str, ...] = ()


_TASK_COLUMNS = frozenset({
    "slug",
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
})

_TASK_TYPED_COLS = frozenset({
    "plan_name",
})

# -- Path Helpers --


def state_path(session_root: str) -> Path:
    """Return the path to the state database file."""
    root = Path(session_root)
    if root.name == STATE_DIR:
        root = root.parent
    return root / STATE_DIR / _STATE_FILE


# -- Event Constants --

# Only events emitted by the Lacustrine kernel + runner
VALID_EVENTS = frozenset({
    # Run lifecycle (bounds events for dgov plan review)
    "run_start",
    "run_completed",
    # Runner lifecycle
    "task_done",
    "task_failed",
    "task_abandoned",
    "shutdown_requested",
    # DAG lifecycle
    "dag_task_dispatched",
    "dag_task_governor_resumed",
    # Review
    "review_pass",
    "review_fail",
    "reviewer_verdict",
    # Merge
    "merge_completed",
    "task_merge_failed",
    "settlement_retry",
    # Worker subprocess
    "worker_log",
    # Semantic Settlement (Phase 1: contract and telemetry)
    "integration_risk_scored",
    "integration_overlap_detected",
    "integration_candidate_passed",
    "integration_candidate_failed",
    "semantic_gate_rejected",
    # Settlement phase telemetry
    "settlement_phase_started",
    "settlement_phase_completed",
    # Self-review
    "self_review_passed",
    "self_review_rejected",
    "self_review_auto_passed",
    "self_review_fix_started",
    "self_review_error",
    # Iteration
    "iteration_fork",
})

_EVENT_TYPED_COLS = frozenset({
    "task_slug",
    "plan_name",
    "action",
    "commit_count",
    "error",
    "reason",
    "merge_sha",
    "branch",
    "new_slug",
    "target_agent",
    "message",
})


__all__ = [
    "STATE_DIR",
    "TASK_STATES",
    "VALID_EVENTS",
    "VALID_TRANSITIONS",
    "_CREATE_EVENTS_TABLE_SQL",
    "_CREATE_LEDGER_TABLE_SQL",
    "_CREATE_SLUG_HISTORY_TABLE_SQL",
    "_CREATE_TABLE_SQL",
    "_EVENT_TYPED_COLS",
    "_SCHEMA_VERSION",
    "_STATE_FILE",
    "_TASK_COLUMNS",
    "_TASK_TYPED_COLS",
    "IllegalTransitionError",
    "WorkerTask",
    "state_path",
]
