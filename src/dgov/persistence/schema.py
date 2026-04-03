"""Schema definitions for persistence layer.

Contains PaneState enum, SQL table definitions, and core data structures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal

# -- Constants --

STATE_DIR = ".dgov"
PROTECTED_FILES = {"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md"}
_STATE_FILE = "state.db"
_NOTIFY_DIR = "notify"

CIRCUIT_BREAKER_THRESHOLD = 3
_SCHEMA_VERSION = 3

# -- Pane State Enum --


class PaneState(StrEnum):
    """Canonical pane states — no others allowed."""

    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    REVIEWED_PASS = "reviewed_pass"
    REVIEWED_FAIL = "reviewed_fail"
    MERGED = "merged"
    TIMED_OUT = "timed_out"
    SUPERSEDED = "superseded"
    CLOSED = "closed"
    ABANDONED = "abandoned"


# Backward-compat set for membership checks and SQLite validation
PANE_STATES = frozenset(PaneState)

# Transition table: enforced in update_pane_state
# Review is mandatory before merge — no direct done→merged or active→merged.
VALID_TRANSITIONS: dict[PaneState, frozenset[PaneState]] = {
    PaneState.ACTIVE: frozenset(
        {
            PaneState.DONE,
            PaneState.FAILED,
            PaneState.ABANDONED,
            PaneState.TIMED_OUT,
            PaneState.CLOSED,
            PaneState.SUPERSEDED,
        }
    ),
    PaneState.DONE: frozenset(
        {
            PaneState.REVIEWED_PASS,
            PaneState.REVIEWED_FAIL,
            PaneState.CLOSED,
            PaneState.SUPERSEDED,
        }
    ),
    PaneState.FAILED: frozenset({PaneState.CLOSED, PaneState.SUPERSEDED}),
    PaneState.REVIEWED_PASS: frozenset({PaneState.MERGED, PaneState.FAILED, PaneState.CLOSED}),
    PaneState.REVIEWED_FAIL: frozenset({PaneState.CLOSED, PaneState.SUPERSEDED}),
    PaneState.MERGED: frozenset({PaneState.CLOSED}),
    PaneState.TIMED_OUT: frozenset(
        {
            PaneState.DONE,
            PaneState.CLOSED,
            PaneState.SUPERSEDED,
        }
    ),
    PaneState.SUPERSEDED: frozenset({PaneState.CLOSED}),
    PaneState.CLOSED: frozenset(),
    PaneState.ABANDONED: frozenset({PaneState.CLOSED, PaneState.SUPERSEDED}),
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


# -- Provenance Union (replaces mutually-exclusive optional fields) --


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


@dataclass(frozen=True, slots=True)
class ProvenanceSuperseded:
    """Pane that was superseded by another (replaced without merge)."""

    by_slug: str
    kind: Literal["superseded"] = "superseded"


@dataclass(frozen=True, slots=True)
class ProvenanceTiered:
    """Pane spawned as part of tiered dispatch (tier-based routing)."""

    tier_id: str
    parent_slug: str  # The pane that spawned this tier child
    kind: Literal["tiered"] = "tiered"


PaneProvenance = ProvenanceOriginal | ProvenanceRetry | ProvenanceSuperseded | ProvenanceTiered


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
    # Provenance: discriminated union encoding lifecycle origin
    # Replaces mutually-exclusive: parent_slug, tier_id, retried_from, superseded_by
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


# Mutation helper for frozen dataclasses:
# from dataclasses import replace; new_pane = replace(pane, state=PaneState.DONE)


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


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
        "provenance",  # Discriminated union: original | retry | superseded | tiered
        "role",
        "state",
    }
)

_PANE_TYPED_COLS = frozenset(
    {
        "file_claims",
        "commit_message",
        "circuit_breaker",
        "retry_count",
        "max_retries",
        "monitor_reason",
        "last_checkpoint",
        "last_hook_match",
        "preserve_reason",
        "preserve_recoverable",
    }
)

# -- SQL Table Definitions --

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS panes (
    slug TEXT PRIMARY KEY,
    prompt TEXT,
    pane_id TEXT,
    agent TEXT,
    project_root TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    created_at REAL,
    owns_worktree INTEGER,
    base_sha TEXT,
    provenance TEXT NOT NULL DEFAULT '{"kind": "original"}',  -- JSON discriminated union
    role TEXT DEFAULT 'worker',
    state TEXT,
    metadata TEXT,
    file_claims TEXT NOT NULL DEFAULT '[]',
    commit_message TEXT DEFAULT NULL,
    circuit_breaker INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    monitor_reason TEXT DEFAULT NULL,
    last_checkpoint TEXT DEFAULT NULL,
    last_hook_match TEXT DEFAULT NULL,
    preserve_reason TEXT DEFAULT NULL,
    preserve_recoverable INTEGER NOT NULL DEFAULT 0
)"""

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    pane TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    commit_count TEXT DEFAULT NULL,
    error TEXT DEFAULT NULL,
    reason TEXT DEFAULT NULL,
    merge_sha TEXT DEFAULT NULL,
    branch TEXT DEFAULT NULL,
    new_slug TEXT DEFAULT NULL,
    target_agent TEXT DEFAULT NULL,
    message TEXT DEFAULT NULL)
"""

_CREATE_DAG_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_file TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_tier INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    definition_json TEXT NOT NULL DEFAULT '{}'
)"""

_CREATE_DAG_TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    pane_slug TEXT,
    worktree_path TEXT,
    file_claims TEXT NOT NULL DEFAULT '[]',
    commit_message TEXT DEFAULT NULL,
    error TEXT,
    UNIQUE(dag_run_id, slug),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_EVALS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    eval_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    evidence TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '[]',
    UNIQUE(dag_run_id, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_UNIT_EVAL_LINKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_unit_eval_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    unit_slug TEXT NOT NULL,
    eval_id TEXT NOT NULL,
    UNIQUE(dag_run_id, unit_slug, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_EVAL_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    eval_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    exit_code INTEGER,
    output TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL,
    UNIQUE(dag_run_id, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_MERGE_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS merge_queue (
    ticket TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    requester TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP
)"""

_CREATE_DECISION_JOURNAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS decision_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    trace_id TEXT,
    model_id TEXT,
    confidence REAL,
    pane_slug TEXT,
    agent_id TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    duration_ms REAL NOT NULL
)"""

_CREATE_SLUG_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS slug_history (
    slug TEXT PRIMARY KEY,
    used_at TEXT NOT NULL)
"""


# -- Path Helpers --


def state_path(session_root: str) -> Path:
    """Return the path to the state database file."""
    return Path(session_root) / STATE_DIR / _STATE_FILE


def notify_dir_path(session_root: str) -> Path:
    """Return the path to the notification directory."""
    return Path(session_root) / STATE_DIR / _NOTIFY_DIR


# -- Event Constants --

VALID_EVENTS = frozenset(
    {
        "dispatch_queued",
        "pane_created",
        "pane_done",
        "pane_failed",
        "pane_resumed",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
        "pane_retry_spawned",
        "pane_auto_retried",
        "pane_blocked",
        "pane_auto_responded",
        "pane_review_pending",
        "checkpoint_created",
        "pane_reviewed_pass",
        "pane_reviewed_fail",
        "review_pass",
        "review_fail",
        "review_fix_started",
        "review_fix_finding",
        "review_fix_completed",
        "mission_pending",
        "mission_running",
        "mission_waiting",
        "mission_reviewing",
        "mission_merging",
        "mission_completed",
        "mission_failed",
        "dag_started",
        "dag_resumed",
        "dag_blocked",
        "dag_cancelled",
        "dag_tier_started",
        "dag_task_dispatched",
        "dag_task_completed",
        "dag_task_failed",
        "dag_task_escalated",
        "dag_tier_completed",
        "dag_completed",
        "dag_failed",
        "merge_enqueued",
        "merge_completed",
        "yap_received",
        "pane_circuit_breaker",
        "monitor_nudge",
        "monitor_auto_complete",
        "monitor_idle_timeout",
        "monitor_blocked",
        "monitor_auto_merge",
        "monitor_auto_retry",
        "monitor_alive",
        "monitor_agent_degraded",
        "monitor_tick",
        "claim_violation",
        "quality_retry",
        "quality_escalate",
        "worker_contradiction",
        "worker_heartbeat",
        "pane_pruned",
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
    "PROTECTED_FILES",
    "_STATE_FILE",
    "_NOTIFY_DIR",
    "CIRCUIT_BREAKER_THRESHOLD",
    "_SCHEMA_VERSION",
    "PaneState",
    "PANE_STATES",
    "VALID_TRANSITIONS",
    "IllegalTransitionError",
    "CompletionTransitionResult",
    "WorkerPane",
    "replace",  # Re-exported: use replace(pane, state=...) for mutations
    "_PANE_COLUMNS",
    "_PANE_TYPED_COLS",
    "_CREATE_TABLE_SQL",
    "_CREATE_EVENTS_TABLE_SQL",
    "_CREATE_DAG_RUNS_TABLE_SQL",
    "_CREATE_DAG_TASKS_TABLE_SQL",
    "_CREATE_DAG_EVALS_TABLE_SQL",
    "_CREATE_DAG_UNIT_EVAL_LINKS_TABLE_SQL",
    "_CREATE_DAG_EVAL_RESULTS_TABLE_SQL",
    "_CREATE_MERGE_QUEUE_TABLE_SQL",
    "_CREATE_DECISION_JOURNAL_TABLE_SQL",
    "_CREATE_SLUG_HISTORY_TABLE_SQL",
    "state_path",
    "notify_dir_path",
    "VALID_EVENTS",
    "_EVENT_TYPED_COLS",
    "_COMPLETION_TARGET_STATES",
    "_SETTLED_PANE_STATES",
]
