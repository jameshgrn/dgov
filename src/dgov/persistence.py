"""State file management and event journal.

Manages .dgov/state.db (pane records and event log via SQLite WAL).
"""

from __future__ import annotations

import json
import logging
import os
import select
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path

from dgov.backend import get_backend

logger = logging.getLogger(__name__)


_NOTIFY_DIR = "notify"


def _notify_dir(session_root: str) -> Path:
    """Return the per-reader notification pipe directory."""
    d = Path(session_root) / STATE_DIR / _NOTIFY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _notify_waiters(session_root: str) -> None:
    """Write a byte to ALL per-reader notify pipes. Non-blocking, fire-and-forget.

    Each reader process creates its own FIFO in .dgov/notify/<pid>.pipe.
    This writes to every pipe so all readers (monitor, dashboard, --wait)
    get woken independently. Stale pipes from dead processes are cleaned up,
    but only if the owning process is actually dead (not just between reads).
    """
    notify = _notify_dir(session_root)
    for pipe_path in notify.iterdir():
        if not pipe_path.name.endswith(".pipe"):
            continue
        try:
            fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x01")
            finally:
                os.close(fd)
        except OSError:
            # Write failed — either no reader right now or process is dead.
            # Only delete the pipe if the owning process is actually dead.
            try:
                pid = int(pipe_path.stem)
                os.kill(pid, 0)  # check if process exists
            except (ValueError, ProcessLookupError):
                # Process is dead — clean up stale pipe
                try:
                    pipe_path.unlink(missing_ok=True)
                except OSError:
                    pass
            except PermissionError:
                pass  # process exists but owned by different user


def _wait_for_notify(session_root: str, timeout: float) -> bool:
    """Block on a per-process notify pipe until data arrives or timeout.

    Each calling process gets its own FIFO in .dgov/notify/<pid>.pipe,
    so multiple readers (monitor, dashboard, --wait) all receive every
    notification independently. Uses poll() — no fd limit, no polling.

    Returns True if notified, False on timeout.
    """
    notify = _notify_dir(session_root)
    pipe_path = notify / f"{os.getpid()}.pipe"
    try:
        os.mkfifo(str(pipe_path))
    except FileExistsError:
        pass

    try:
        fd = os.open(str(pipe_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            events = poller.poll(int(timeout * 1000))
            if events:
                os.read(fd, 4096)  # drain pipe
                return True
            return False
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # Pipe broken — recreate next call
        try:
            pipe_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# -- Connection cache (per db_path + thread) --

_conn_cache: dict[tuple[str, int], sqlite3.Connection] = {}
_conn_lock = threading.Lock()

# -- Event log --

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
        "monitor_tick",
        "claim_violation",
        "quality_retry",
        "quality_escalate",
        "evals_verified",
        "worker_contradiction",
    }
)


_EVENT_TYPED_COLS = frozenset(
    {
        "error",
        "reason",
        "merge_sha",
        "branch",
        "new_slug",
        "target_agent",
        "message",
    }
)


def emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Write a structured event to the events table in state.db.

    Known kwargs are written to typed columns. Remaining kwargs go to the
    data JSON blob as overflow. Best-effort: logs a warning on lock
    contention instead of crashing.
    """
    from datetime import datetime, timezone

    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")

    def _do() -> None:
        conn = _get_db(session_root)
        ts = datetime.now(timezone.utc).isoformat()

        typed = {k: str(v) for k, v in kwargs.items() if k in _EVENT_TYPED_COLS}
        overflow = {k: v for k, v in kwargs.items() if k not in _EVENT_TYPED_COLS}
        data = json.dumps(overflow, default=str) if overflow else "{}"

        cols = ["ts", "event", "pane", "data", *typed.keys()]
        placeholders = ", ".join("?" for _ in cols)
        vals = [ts, event, pane, data, *typed.values()]
        conn.execute(
            f"INSERT INTO events ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()

    try:
        _retry_on_lock(_do)
    except sqlite3.OperationalError:
        logger.warning("emit_event(%s, %s) dropped — database locked", event, pane)

    # Touch sentinel to wake kqueue watchers
    try:
        _notify_waiters(session_root)
    except OSError:
        pass


def read_events(
    session_root: str,
    slug: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read events from the SQLite events table, optionally filtered by slug."""
    _typed = ", ".join(_EVENT_TYPED_COLS)
    _select = f"ts, event, pane, data, {_typed}"
    conn = _get_db(session_root)
    if slug is not None:
        if limit is not None:
            rows = conn.execute(
                f"SELECT {_select} FROM events WHERE pane = ? ORDER BY id DESC LIMIT ?",
                (slug, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_select} FROM events WHERE pane = ? ORDER BY id",
                (slug,),
            ).fetchall()
    else:
        if limit is not None:
            rows = conn.execute(
                f"SELECT {_select} FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_select} FROM events ORDER BY id",
            ).fetchall()
    typed_col_names = list(_EVENT_TYPED_COLS)
    events = []
    # For limited queries we fetch newest-first for performance, then reverse to keep
    # chronological (oldest-first) order at the API boundary.
    if limit is not None:
        rows = list(reversed(rows))
    for row in rows:
        ts, event, pane, data_str = row[0], row[1], row[2], row[3]
        ev = {"ts": ts, "event": event, "pane": pane}
        try:
            ev.update(json.loads(data_str))
        except (json.JSONDecodeError, TypeError):
            pass
        # Overlay typed columns (non-empty values win over JSON blob)
        for i, col in enumerate(typed_col_names):
            val = row[4 + i]
            if val:
                ev[col] = val
        events.append(ev)
    return events


def latest_event_id(session_root: str) -> int:
    """Return the latest event row id, or 0 if the journal is empty."""
    conn = _get_db(session_root)
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0]) if row is not None else 0


def wait_for_events(
    session_root: str,
    *,
    after_id: int,
    panes: tuple[str, ...] = (),
    event_types: tuple[str, ...] = (),
    timeout_s: float = 5.0,
) -> list[dict]:
    """Wait for new events after *after_id*, optionally filtered by pane and type.

    Uses kqueue-based file watching on .dgov/done/ for zero-polling wakeup.
    """
    if timeout_s < 0:
        raise ValueError("timeout_s must be non-negative")

    conn = _get_db(session_root)
    pane_filters = tuple(dict.fromkeys(p for p in panes if p))
    event_filters = tuple(dict.fromkeys(e for e in event_types if e))

    query = ["SELECT id, ts, event, pane, data FROM events WHERE id > ?"]
    params: list[object] = [after_id]

    if pane_filters:
        placeholders = ", ".join("?" for _ in pane_filters)
        query.append(f"AND pane IN ({placeholders})")
        params.extend(pane_filters)
    if event_filters:
        placeholders = ", ".join("?" for _ in event_filters)
        query.append(f"AND event IN ({placeholders})")
        params.extend(event_filters)

    query.append("ORDER BY id")
    sql = " ".join(query)
    start = time.monotonic()
    deadline = start + timeout_s

    while True:
        rows = conn.execute(sql, tuple(params)).fetchall()
        if rows:
            events: list[dict] = []
            for event_id, ts, event, pane, data_str in rows:
                ev = {"id": event_id, "ts": ts, "event": event, "pane": pane}
                try:
                    ev.update(json.loads(data_str))
                except (json.JSONDecodeError, TypeError):
                    pass
                events.append(ev)
            return events

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        _wait_for_notify(session_root, min(remaining, timeout_s))


# -- Pane record --


# Canonical pane states — no others allowed
PANE_STATES = frozenset(
    {
        "active",
        "done",
        "failed",
        "reviewed_pass",
        "reviewed_fail",
        "merged",
        "merge_conflict",
        "timed_out",
        "escalated",
        "superseded",
        "closed",
        "abandoned",
    }
)


# Pane hierarchy fields
class PANE_TIER:
    GOVERNOR = "governor"
    MANAGER = "manager"
    WORKER = "worker"


# Transition table: 12 states, enforced in update_pane_state
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset(
        {"done", "failed", "abandoned", "timed_out", "closed", "escalated", "superseded", "merged"}
    ),
    "done": frozenset(
        {"reviewed_pass", "reviewed_fail", "merged", "merge_conflict", "closed", "superseded"}
    ),
    "failed": frozenset({"closed", "superseded", "escalated"}),
    "reviewed_pass": frozenset({"merged", "merge_conflict", "closed"}),
    "reviewed_fail": frozenset({"closed", "superseded", "escalated"}),
    "merged": frozenset({"closed"}),
    "merge_conflict": frozenset({"merged", "closed", "escalated"}),
    "timed_out": frozenset({"done", "merged", "closed", "superseded", "escalated"}),
    "escalated": frozenset({"closed"}),
    "superseded": frozenset({"closed"}),
    "closed": frozenset(),
    "abandoned": frozenset({"closed", "superseded", "escalated"}),
}


class IllegalTransitionError(ValueError):
    def __init__(self, current: str, target: str, slug: str):
        self.current = current
        self.target = target
        self.slug = slug
        super().__init__(f"Illegal state transition for '{slug}': {current} -> {target}")


@dataclass(frozen=True)
class CompletionTransitionResult:
    state: str
    changed: bool


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


@dataclass
class WorkerPane:
    slug: str
    prompt: str
    pane_id: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str = ""
    parent_slug: str = ""
    tier_id: str = ""
    role: str = "worker"
    state: str = "active"
    file_claims: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_state(self.state)


# -- State DB helpers --

STATE_DIR = ".dgov"
PROTECTED_FILES = {"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md"}
_STATE_FILE = "state.db"

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
        "parent_slug",
        "tier_id",
        "role",
        "state",
    }
)

_COMPLETION_TARGET_STATES = frozenset({"done", "failed", "abandoned", "timed_out"})
_SETTLED_PANE_STATES = PANE_STATES - {"active"}


def _maybe_update_pane_title(session_root: str, slug: str, new_state: str) -> None:
    """Update the pane title after a persisted state change."""
    # Skip for terminal states — pane is dead, title update would fork tmux for nothing.
    if new_state in ("merged", "closed", "superseded"):
        return

    pane = get_pane(session_root, slug)
    if not pane:
        return

    pane_id = pane.get("pane_id", "")
    agent = pane.get("agent", "")
    project_root = pane.get("project_root", "")
    if not pane_id:
        return

    from dgov.lifecycle import _build_pane_title

    try:
        title = _build_pane_title(agent, slug, project_root, state=pane.get("state", new_state))
        get_backend().set_title(pane_id, title)
    except (RuntimeError, OSError):
        pass  # pane may already be dead


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
    parent_slug TEXT,
    tier_id TEXT,
    role TEXT DEFAULT 'worker',
    state TEXT,
    metadata TEXT,
    landing INTEGER NOT NULL DEFAULT 0,
    file_claims TEXT NOT NULL DEFAULT '[]',
    circuit_breaker INTEGER NOT NULL DEFAULT 0,
    retried_from TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    monitor_reason TEXT NOT NULL DEFAULT '',
    last_checkpoint TEXT NOT NULL DEFAULT '',
    last_hook_match TEXT NOT NULL DEFAULT '',
    preserve_reason TEXT NOT NULL DEFAULT '',
    preserve_recoverable INTEGER NOT NULL DEFAULT 0
)"""

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    pane TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    merge_sha TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    new_slug TEXT NOT NULL DEFAULT '',
    target_agent TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '')
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


CIRCUIT_BREAKER_THRESHOLD = 3


def state_path(session_root: str) -> Path:
    return Path(session_root) / STATE_DIR / _STATE_FILE


def _get_db(session_root: str) -> sqlite3.Connection:
    """Return a cached SQLite connection for this (db_path, thread).

    First call per thread creates the connection, sets WAL mode,
    busy_timeout, and runs CREATE TABLE.  Subsequent calls return
    the cached connection.
    """
    db_path = str(state_path(session_root))
    key = (db_path, threading.get_ident())

    with _conn_lock:
        conn = _conn_cache.get(key)
        if conn is not None:
            return conn

    # Outside the lock — only one thread will ever hit this for a given key.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_EVENTS_TABLE_SQL)
    conn.execute(_CREATE_DAG_RUNS_TABLE_SQL)
    conn.execute(_CREATE_DAG_TASKS_TABLE_SQL)
    conn.execute(_CREATE_DAG_EVALS_TABLE_SQL)
    conn.execute(_CREATE_DAG_UNIT_EVAL_LINKS_TABLE_SQL)
    conn.execute(_CREATE_MERGE_QUEUE_TABLE_SQL)
    conn.execute(_CREATE_DECISION_JOURNAL_TABLE_SQL)
    conn.execute(_CREATE_SLUG_HISTORY_TABLE_SQL)

    # Spans + tool traces (Phase 4 observability)
    from dgov.spans import (
        CREATE_ARCHIVED_PANES_SQL,
        CREATE_LEDGER_IDX,
        CREATE_LEDGER_SQL,
        CREATE_PROMPTS_SQL,
        CREATE_SPANS_IDX_KIND,
        CREATE_SPANS_IDX_TRACE,
        CREATE_SPANS_SQL,
        CREATE_TOOL_TRACES_IDX,
        CREATE_TOOL_TRACES_SQL,
        CREATE_TRANSCRIPTS_SQL,
    )

    conn.execute(CREATE_SPANS_SQL)
    conn.execute(CREATE_SPANS_IDX_TRACE)
    conn.execute(CREATE_SPANS_IDX_KIND)
    conn.execute(CREATE_TOOL_TRACES_SQL)
    conn.execute(CREATE_TOOL_TRACES_IDX)
    conn.execute(CREATE_PROMPTS_SQL)
    conn.execute(CREATE_ARCHIVED_PANES_SQL)
    conn.execute(CREATE_TRANSCRIPTS_SQL)
    conn.execute(CREATE_LEDGER_SQL)
    conn.execute(CREATE_LEDGER_IDX)

    # Migrate: add hierarchy columns if missing
    for col, default in [("parent_slug", "''"), ("tier_id", "''"), ("role", "'worker'")]:
        try:
            conn.execute(f"ALTER TABLE panes ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate: add decision journal columns if missing
    for col, coltype in [
        ("model_id", "TEXT"),
        ("confidence", "REAL"),
        ("pane_slug", "TEXT"),
        ("agent_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE decision_journal ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate: drop unused metadata_json by ignoring it (SQLite cannot DROP COLUMN before 3.35)
    # No action needed — column is never read. Left in schema for backward compat.

    # Migrate: add typed pane metadata columns
    _pane_meta_cols = [
        ("landing", "INTEGER NOT NULL DEFAULT 0"),
        ("file_claims", "TEXT NOT NULL DEFAULT '[]'"),
        ("circuit_breaker", "INTEGER NOT NULL DEFAULT 0"),
        ("retried_from", "TEXT NOT NULL DEFAULT ''"),
        ("superseded_by", "TEXT NOT NULL DEFAULT ''"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("max_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("monitor_reason", "TEXT NOT NULL DEFAULT ''"),
        ("last_checkpoint", "TEXT NOT NULL DEFAULT ''"),
        ("last_hook_match", "TEXT NOT NULL DEFAULT ''"),
        ("preserve_reason", "TEXT NOT NULL DEFAULT ''"),
        ("preserve_recoverable", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, coldef in _pane_meta_cols:
        try:
            conn.execute(f"ALTER TABLE panes ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass

    # Migrate: add typed event columns
    _event_cols = [
        ("error", "TEXT NOT NULL DEFAULT ''"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("merge_sha", "TEXT NOT NULL DEFAULT ''"),
        ("branch", "TEXT NOT NULL DEFAULT ''"),
        ("new_slug", "TEXT NOT NULL DEFAULT ''"),
        ("target_agent", "TEXT NOT NULL DEFAULT ''"),
        ("message", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, coldef in _event_cols:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass

    # Migrate: add typed archived_panes columns (replace metadata blob)
    _archive_cols = [
        ("landing", "INTEGER NOT NULL DEFAULT 0"),
        ("file_claims", "TEXT NOT NULL DEFAULT '[]'"),
        ("circuit_breaker", "INTEGER NOT NULL DEFAULT 0"),
        ("retried_from", "TEXT NOT NULL DEFAULT ''"),
        ("superseded_by", "TEXT NOT NULL DEFAULT ''"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("max_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("monitor_reason", "TEXT NOT NULL DEFAULT ''"),
        ("last_checkpoint", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, coldef in _archive_cols:
        try:
            conn.execute(f"ALTER TABLE archived_panes ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass

    conn.commit()

    with _conn_lock:
        # Another racer may have inserted; prefer the first one.
        existing = _conn_cache.get(key)
        if existing is not None:
            conn.close()
            return existing
        _conn_cache[key] = conn
    return conn


_LOCK_RETRIES = 20
_LOCK_BACKOFF_S = 0.5


def _retry_on_lock(fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """Call *fn* with retries on 'database is locked' errors."""
    for attempt in range(_LOCK_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == _LOCK_RETRIES - 1:
                raise
            logger.debug("database locked, retry %d/%d", attempt + 1, _LOCK_RETRIES)
            time.sleep(_LOCK_BACKOFF_S * (attempt + 1))
    return None  # unreachable, but keeps type checkers happy


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a pane dict, merging any legacy metadata extras."""
    d = dict(row)
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    # Legacy metadata JSON — merge for backward compat, but typed columns win
    metadata = d.pop("metadata", None)
    if metadata:
        try:
            legacy = json.loads(str(metadata))
            # Only fill keys not already present as typed columns
            for k, v in legacy.items():
                if k not in d:
                    d[k] = v
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupt pane metadata for slug=%s: %.100s", d.get("slug", "?"), metadata
            )
    return d


def _insert_pane_dict(conn: sqlite3.Connection, pane_dict: dict) -> None:
    """Insert a pane dict into the database, separating known columns from metadata."""
    values: dict = {}
    extras: dict = {}
    for k, v in pane_dict.items():
        if k in _PANE_COLUMNS:
            values[k] = v
        else:
            extras[k] = v

    if "owns_worktree" in values and isinstance(values["owns_worktree"], bool):
        values["owns_worktree"] = int(values["owns_worktree"])

    values["metadata"] = json.dumps(extras) if extras else None

    cols = ", ".join(values.keys())
    placeholders = ", ".join("?" * len(values))
    conn.execute(
        f"INSERT OR REPLACE INTO panes ({cols}) VALUES ({placeholders})",
        list(values.values()),
    )


def add_pane(session_root: str, pane: WorkerPane) -> None:
    def _do() -> None:
        conn = _get_db(session_root)
        _insert_pane_dict(conn, asdict(pane))
        conn.commit()

    _retry_on_lock(_do)


def remove_pane(session_root: str, slug: str) -> None:
    def _do() -> None:
        conn = _get_db(session_root)
        # Archive before delete
        row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
        if row:
            cols = [d[0] for d in conn.execute("SELECT * FROM panes LIMIT 0").description]
            pane_dict = dict(zip(cols, row))
            try:
                from dgov.spans import archive_pane

                archive_pane(session_root, pane_dict)
            except Exception:
                pass  # archive failure must not block deletion
        conn.execute("DELETE FROM panes WHERE slug = ?", (slug,))
        # Record slug in history for unique allocation tracking
        conn.execute(
            "INSERT OR IGNORE INTO slug_history (slug, used_at) VALUES (?, ?)",
            (slug, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()

    _retry_on_lock(_do)


def get_slug_history(session_root: str) -> set[str]:
    """Return all slugs that have ever been used (including closed/removed panes)."""
    conn = _get_db(session_root)
    rows = conn.execute("SELECT slug FROM slug_history").fetchall()
    return {row[0] for row in rows}


def get_pane(session_root: str, slug: str) -> dict | None:
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
    return _row_to_dict(row) if row else None


def all_panes(session_root: str) -> list[dict]:
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes").fetchall()
    return [_row_to_dict(row) for row in rows]


def list_panes_slim(session_root: str) -> list[dict]:
    """List all panes without full prompt text (for hot-path display).

    Returns the first 200 characters of each prompt instead of the full blob.
    """
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes").fetchall()
    result = [_row_to_dict(row) for row in rows]
    for r in result:
        if r.get("prompt") and len(r["prompt"]) > 200:
            r["prompt"] = r["prompt"][:200]
    return result


def get_pane_prompt(session_root: str, slug: str) -> str:
    """Get just the prompt text for a single pane."""
    conn = _get_db(session_root)
    row = conn.execute("SELECT prompt FROM panes WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else ""


def update_pane_state(session_root: str, slug: str, new_state: str, force: bool = False) -> None:
    """Update the state field of a pane record.

    Enforces VALID_TRANSITIONS unless *force* is True.
    Same-state transitions are no-ops.
    Uses an atomic UPDATE … WHERE to avoid read-check-write races.
    Raises IllegalTransitionError for disallowed transitions.
    """
    _validate_state(new_state)

    def _do() -> None:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row

        if force:
            # Skip transition validation — unconditional update.
            cur = conn.execute(
                "UPDATE panes SET state = ? WHERE slug = ? AND state != ?",
                (new_state, slug, new_state),
            )
        else:
            # Build the set of states that are allowed to transition to new_state.
            allowed_from = [
                st for st, targets in VALID_TRANSITIONS.items() if new_state in targets
            ]
            if not allowed_from:
                # No state can legally reach new_state.  Same-state is still a no-op.
                row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                return

            placeholders = ", ".join("?" * len(allowed_from))
            cur = conn.execute(
                f"UPDATE panes SET state = ? WHERE slug = ? AND state IN ({placeholders})",
                [new_state, slug, *allowed_from],
            )

            if cur.rowcount == 0:
                # Either slug missing, already at new_state, or illegal transition.
                row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                # slug missing or already at new_state — no-op.
                return

        conn.commit()

    _retry_on_lock(_do)
    _maybe_update_pane_title(session_root, slug, new_state)


def settle_completion_state(
    session_root: str,
    slug: str,
    new_state: str,
    *,
    allow_abandoned: bool = False,
) -> CompletionTransitionResult:
    """Set a completion state without raising on late terminal races.

    This helper is intentionally narrow: it only applies to completion-path
    states that may race across wait, done detection, manual signals, and
    timeout handling. Normal transition enforcement remains in
    ``update_pane_state()``.

    Returns the persisted state and whether this call changed it.
    """
    _validate_state(new_state)
    if new_state not in _COMPLETION_TARGET_STATES:
        raise ValueError(
            f"settle_completion_state only supports {_COMPLETION_TARGET_STATES}, got {new_state!r}"
        )

    changed = False

    def _do() -> CompletionTransitionResult:
        nonlocal changed

        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row

        allowed_from = {
            state for state, targets in VALID_TRANSITIONS.items() if new_state in targets
        }
        if allow_abandoned:
            allowed_from.add("abandoned")

        placeholders = ", ".join("?" * len(allowed_from))
        cur = conn.execute(
            f"UPDATE panes SET state = ? WHERE slug = ? AND state IN ({placeholders})",
            [new_state, slug, *sorted(allowed_from)],
        )

        if cur.rowcount:
            conn.commit()
            changed = True
            return CompletionTransitionResult(state=new_state, changed=True)

        row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
        conn.commit()

        if row is None:
            return CompletionTransitionResult(state=new_state, changed=False)

        current_state = row["state"]
        if current_state == new_state:
            return CompletionTransitionResult(state=current_state, changed=False)

        if current_state in _SETTLED_PANE_STATES:
            return CompletionTransitionResult(state=current_state, changed=False)

        raise IllegalTransitionError(current_state, new_state, slug)

    result = _retry_on_lock(_do)
    if changed:
        _maybe_update_pane_title(session_root, slug, new_state)
    return result


_PANE_TYPED_COLS = frozenset(
    {
        "landing",
        "file_claims",
        "circuit_breaker",
        "retried_from",
        "superseded_by",
        "retry_count",
        "max_retries",
        "monitor_reason",
        "last_checkpoint",
        "last_hook_match",
        "preserve_reason",
        "preserve_recoverable",
    }
)


def set_pane_metadata(session_root: str, slug: str, **kwargs: object) -> None:
    """Update metadata fields on a specific pane.

    Known keys are written to typed columns. Unknown keys fall back to
    the legacy metadata JSON blob (logged as a warning).
    """
    if not kwargs:
        return

    def _do() -> None:
        conn = _get_db(session_root)
        typed_sets: list[str] = []
        typed_vals: list[object] = []

        for k, v in kwargs.items():
            if k in _PANE_TYPED_COLS:
                if isinstance(v, dict | list):
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(json.dumps(v, default=str))
                else:
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(v)
            else:
                raise ValueError(
                    f"set_pane_metadata: unknown key {k!r} for slug={slug}. "
                    f"Add it to _PANE_TYPED_COLS and the panes schema first."
                )

        if typed_sets:
            typed_vals.append(slug)
            sql = f"UPDATE panes SET {', '.join(typed_sets)} WHERE slug = ?"
            conn.execute(sql, typed_vals)
        conn.commit()

    _retry_on_lock(_do)


def get_preserved_artifacts(pane_record: dict | None) -> dict | None:
    """Return preserved-artifact metadata when present."""
    if not pane_record:
        return None
    reason = pane_record.get("preserve_reason", "")
    if not reason:
        return None
    return {
        "reason": reason,
        "recoverable": bool(pane_record.get("preserve_recoverable", 0)),
    }


def mark_preserved_artifacts(
    session_root: str,
    slug: str,
    *,
    reason: str,
    recoverable: bool,
    state: str | None = None,
    failure_stage: str | None = None,
    preserved_paths: list[str] | tuple[str, ...] = (),
) -> None:
    """Persist preserved-artifact metadata for a pane kept for inspection."""
    pane = get_pane(session_root, slug)
    if pane is None:
        return

    paths: list[str] = []
    for candidate in (
        *preserved_paths,
        str(pane.get("worktree_path", "")),
        str(Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"),
    ):
        if candidate and candidate not in paths:
            paths.append(candidate)

    set_pane_metadata(
        session_root,
        slug,
        preserve_reason=reason,
        preserve_recoverable=int(recoverable),
    )


def clear_preserved_artifacts(session_root: str, slug: str) -> None:
    """Remove preserved-artifact metadata once a pane is resumed or cleaned."""
    set_pane_metadata(session_root, slug, preserve_reason="", preserve_recoverable=0)


def record_failure(session_root: str, slug: str, failure_hash: str) -> int:
    """Record a failure hash for circuit-breaker detection.

    Tracks failure hashes in pane metadata under ``failure_hashes``
    (a JSON object mapping hash -> count).  Returns the count for
    *failure_hash* after incrementing.
    """

    def _do() -> int:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT metadata FROM panes WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return 0
        meta: dict = {}
        raw = row["metadata"]
        if raw:
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        hashes = meta.get("failure_hashes", {})
        if not isinstance(hashes, dict):
            hashes = {}
        hashes[failure_hash] = hashes.get(failure_hash, 0) + 1
        count: int = hashes[failure_hash]
        meta["failure_hashes"] = hashes
        conn.execute(
            "UPDATE panes SET metadata = ? WHERE slug = ?",
            (json.dumps(meta), slug),
        )
        conn.commit()
        return count

    return _retry_on_lock(_do)


def get_child_panes(session_root: str, parent_slug: str) -> list[dict]:
    """Return all panes whose parent_slug matches *parent_slug*."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes WHERE parent_slug = ?", (parent_slug,)).fetchall()
    return [_row_to_dict(row) for row in rows]


def replace_all_panes(session_root: str, panes: list[dict] | dict) -> None:
    """Replace all panes in the database with the given list.

    Intended for test setup where you need to establish a known state.
    Each dict should have at least a ``slug`` key.
    Accepts either a list of dicts or a dict with a ``panes`` key.
    """
    if isinstance(panes, dict):
        panes = panes.get("panes", [])

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM panes")
        for pane_dict in panes:
            _insert_pane_dict(conn, pane_dict)
        conn.commit()

    _retry_on_lock(_do)


# -- DAG run persistence --


def ensure_dag_tables(session_root: str) -> None:
    """Ensure dag_runs and dag_tasks tables exist."""
    conn = _get_db(session_root)
    conn.execute(_CREATE_DAG_RUNS_TABLE_SQL)
    conn.execute(_CREATE_DAG_TASKS_TABLE_SQL)
    conn.execute(_CREATE_DAG_EVALS_TABLE_SQL)
    conn.execute(_CREATE_DAG_UNIT_EVAL_LINKS_TABLE_SQL)
    conn.execute(_CREATE_DAG_EVAL_RESULTS_TABLE_SQL)
    conn.commit()


def create_dag_run(
    session_root: str,
    dag_file: str,
    started_at: str,
    status: str,
    current_tier: int,
    state_json: dict,
    definition_json: dict | None = None,
) -> int:
    """Insert a new DAG run row and return its id."""

    def _do() -> int:
        conn = _get_db(session_root)
        cur = conn.execute(
            "INSERT INTO dag_runs"
            " (dag_file, started_at, status, current_tier, state_json, definition_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                dag_file,
                started_at,
                status,
                current_tier,
                json.dumps(state_json),
                json.dumps(definition_json) if definition_json else "{}",
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    return _retry_on_lock(_do)


def get_open_dag_run(session_root: str, dag_file: str) -> dict | None:
    """Find an unfinished DAG run for the given absolute dag_file path."""
    conn = _get_db(session_root)
    row = conn.execute(
        "SELECT id, dag_file, started_at, status, current_tier, state_json, definition_json"
        " FROM dag_runs"
        " WHERE dag_file = ? AND status NOT IN (?, ?, ?)"
        " ORDER BY id DESC LIMIT 1",
        (dag_file, "completed", "failed", "cancelled"),
    ).fetchone()
    if row is None:
        return None
    run = {
        "id": row[0],
        "dag_file": row[1],
        "started_at": row[2],
        "status": row[3],
        "current_tier": row[4],
        "state_json": json.loads(row[5]),
        "definition_json": json.loads(row[6]),
    }
    run["evals"] = list_dag_evals(session_root, run["id"])
    run["unit_eval_links"] = list_dag_unit_eval_links(session_root, run["id"])
    run["eval_results"] = list_eval_results(session_root, run["id"])
    return run


def get_dag_run(session_root: str, dag_run_id: int) -> dict | None:
    """Fetch a DAG run by id."""
    conn = _get_db(session_root)
    row = conn.execute(
        "SELECT id, dag_file, started_at, status, current_tier, state_json, definition_json"
        " FROM dag_runs WHERE id = ?",
        (dag_run_id,),
    ).fetchone()
    if row is None:
        return None
    run = {
        "id": row[0],
        "dag_file": row[1],
        "started_at": row[2],
        "status": row[3],
        "current_tier": row[4],
        "state_json": json.loads(row[5]),
        "definition_json": json.loads(row[6]),
    }
    run["evals"] = list_dag_evals(session_root, run["id"])
    run["unit_eval_links"] = list_dag_unit_eval_links(session_root, run["id"])
    run["eval_results"] = list_eval_results(session_root, run["id"])
    return run


def update_dag_run(
    session_root: str,
    dag_run_id: int,
    *,
    status: str | None = None,
    current_tier: int | None = None,
    state_json: dict | None = None,
) -> None:
    """Update mutable fields on a DAG run."""
    sets: list[str] = []
    vals: list[object] = []
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if current_tier is not None:
        sets.append("current_tier = ?")
        vals.append(current_tier)
    if state_json is not None:
        sets.append("state_json = ?")
        vals.append(json.dumps(state_json))
    if not sets:
        return
    vals.append(dag_run_id)

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute(f"UPDATE dag_runs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()

    _retry_on_lock(_do)


def list_active_dag_runs(session_root: str) -> list[dict]:
    """List all DAG runs not in a terminal state."""
    conn = _get_db(session_root)
    rows = conn.execute(
        "SELECT id, dag_file, started_at, status, current_tier, state_json, definition_json"
        " FROM dag_runs"
        " WHERE status NOT IN ('completed', 'failed', 'cancelled')"
    ).fetchall()
    runs = [
        {
            "id": r[0],
            "dag_file": r[1],
            "started_at": r[2],
            "status": r[3],
            "current_tier": r[4],
            "state_json": json.loads(r[5]),
            "definition_json": json.loads(r[6]),
        }
        for r in rows
    ]
    for run in runs:
        run["evals"] = list_dag_evals(session_root, run["id"])
        run["unit_eval_links"] = list_dag_unit_eval_links(session_root, run["id"])
        run["eval_results"] = list_eval_results(session_root, run["id"])
    return runs


def replace_dag_plan_contract(
    session_root: str,
    dag_run_id: int,
    *,
    evals: list[dict],
    unit_eval_links: list[dict],
) -> None:
    """Replace persisted eval contract rows for a DAG run."""

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM dag_unit_eval_links WHERE dag_run_id = ?", (dag_run_id,))
        conn.execute("DELETE FROM dag_evals WHERE dag_run_id = ?", (dag_run_id,))
        for plan_eval in evals:
            conn.execute(
                """INSERT INTO dag_evals
                   (dag_run_id, eval_id, kind, statement, evidence, scope)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    dag_run_id,
                    plan_eval["eval_id"],
                    plan_eval["kind"],
                    plan_eval["statement"],
                    plan_eval["evidence"],
                    json.dumps(plan_eval.get("scope", [])),
                ),
            )
        for link in unit_eval_links:
            conn.execute(
                """INSERT INTO dag_unit_eval_links
                   (dag_run_id, unit_slug, eval_id)
                   VALUES (?, ?, ?)""",
                (dag_run_id, link["unit_slug"], link["eval_id"]),
            )
        conn.commit()

    _retry_on_lock(_do)


def list_dag_evals(session_root: str, dag_run_id: int) -> list[dict]:
    """List persisted eval rows for a DAG run."""
    conn = _get_db(session_root)
    rows = conn.execute(
        "SELECT eval_id, kind, statement, evidence, scope"
        " FROM dag_evals WHERE dag_run_id = ? ORDER BY eval_id",
        (dag_run_id,),
    ).fetchall()
    return [
        {
            "eval_id": row[0],
            "kind": row[1],
            "statement": row[2],
            "evidence": row[3],
            "scope": json.loads(row[4]),
        }
        for row in rows
    ]


def list_dag_unit_eval_links(session_root: str, dag_run_id: int) -> list[dict]:
    """List unit-to-eval links for a DAG run."""
    conn = _get_db(session_root)
    rows = conn.execute(
        "SELECT unit_slug, eval_id"
        " FROM dag_unit_eval_links WHERE dag_run_id = ? ORDER BY unit_slug, eval_id",
        (dag_run_id,),
    ).fetchall()
    return [{"unit_slug": row[0], "eval_id": row[1]} for row in rows]


def record_eval_result(
    session_root: str,
    dag_run_id: int,
    eval_id: str,
    passed: bool,
    exit_code: int | None,
    output: str,
) -> None:
    """Record an eval evidence check result."""
    from datetime import datetime, timezone

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute(
            """INSERT OR REPLACE INTO dag_eval_results
               (dag_run_id, eval_id, passed, exit_code, output, verified_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                dag_run_id,
                eval_id,
                1 if passed else 0,
                exit_code,
                output[-2000:] if len(output) > 2000 else output,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    _retry_on_lock(_do)


def list_eval_results(session_root: str, dag_run_id: int) -> list[dict]:
    """List eval evidence check results for a DAG run."""
    conn = _get_db(session_root)
    rows = conn.execute(
        "SELECT eval_id, passed, exit_code, output, verified_at"
        " FROM dag_eval_results WHERE dag_run_id = ? ORDER BY eval_id",
        (dag_run_id,),
    ).fetchall()
    return [
        {
            "eval_id": row[0],
            "passed": bool(row[1]),
            "exit_code": row[2],
            "output": row[3],
            "verified_at": row[4],
        }
        for row in rows
    ]


def upsert_dag_task(
    session_root: str,
    dag_run_id: int,
    slug: str,
    status: str,
    agent: str,
    attempt: int = 1,
    pane_slug: str | None = None,
    error: str | None = None,
) -> None:
    """Insert or update a DAG task row."""

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute(
            """INSERT INTO dag_tasks (dag_run_id, slug, status, agent, attempt, pane_slug, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(dag_run_id, slug) DO UPDATE SET
                 status=excluded.status, agent=excluded.agent,
                 attempt=excluded.attempt, pane_slug=excluded.pane_slug,
                 error=excluded.error""",
            (dag_run_id, slug, status, agent, attempt, pane_slug, error),
        )
        conn.commit()

    _retry_on_lock(_do)


def list_dag_tasks(session_root: str, dag_run_id: int) -> list[dict]:
    """List all task rows for a DAG run."""
    conn = _get_db(session_root)
    rows = conn.execute(
        "SELECT slug, status, agent, attempt, pane_slug, error"
        " FROM dag_tasks WHERE dag_run_id = ? ORDER BY slug",
        (dag_run_id,),
    ).fetchall()
    return [
        {
            "slug": r[0],
            "status": r[1],
            "agent": r[2],
            "attempt": r[3],
            "pane_slug": r[4],
            "error": r[5],
        }
        for r in rows
    ]


def get_dag_task(session_root: str, dag_run_id: int, slug: str) -> dict | None:
    """Return one DAG task row for a DAG run, or None if missing."""
    conn = _get_db(session_root)
    row = conn.execute(
        "SELECT slug, status, agent, attempt, pane_slug, error"
        " FROM dag_tasks WHERE dag_run_id = ? AND slug = ?",
        (dag_run_id, slug),
    ).fetchone()
    if row is None:
        return None
    return {
        "slug": row[0],
        "status": row[1],
        "agent": row[2],
        "attempt": row[3],
        "pane_slug": row[4],
        "error": row[5],
    }


# -- Merge queue --


def enqueue_merge(session_root: str, branch: str, requester: str) -> str:
    """Add a merge request to the queue. Returns ticket ID."""
    import uuid

    ticket = uuid.uuid4().hex[:8]
    db = _get_db(session_root)
    db.execute(
        "INSERT INTO merge_queue (ticket, branch, requester) VALUES (?, ?, ?)",
        (ticket, branch, requester),
    )
    db.commit()
    return ticket


def claim_next_merge(session_root: str) -> dict | None:
    """Claim the next pending merge. Returns {ticket, branch, requester} or None.

    Uses BEGIN IMMEDIATE for serialization — only one caller can claim at a time.
    """
    db = _get_db(session_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT ticket, branch, requester FROM merge_queue "
            "WHERE status = 'pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            db.execute("COMMIT")
            return None
        ticket, branch, requester = row
        db.execute(
            "UPDATE merge_queue SET status = 'processing' WHERE ticket = ?",
            (ticket,),
        )
        db.execute("COMMIT")
        return {"ticket": ticket, "branch": branch, "requester": requester}
    except Exception:
        db.execute("ROLLBACK")
        raise


def complete_merge(session_root: str, ticket: str, success: bool, result_json: str = "{}") -> None:
    """Record merge result for a claimed ticket."""
    db = _get_db(session_root)
    status = "done" if success else "failed"
    db.execute(
        "UPDATE merge_queue SET status = ?, result = ?, processed_at = CURRENT_TIMESTAMP "
        "WHERE ticket = ?",
        (status, result_json, ticket),
    )
    db.commit()


def list_merge_queue(session_root: str, status: str | None = None) -> list[dict]:
    """List merge queue entries, optionally filtered by status."""
    db = _get_db(session_root)
    if status:
        rows = db.execute(
            "SELECT ticket, branch, requester, status, result, created_at, processed_at "
            "FROM merge_queue WHERE status = ? ORDER BY created_at",
            (status,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT ticket, branch, requester, status, result, created_at, processed_at "
            "FROM merge_queue ORDER BY created_at"
        ).fetchall()
    cols = ["ticket", "branch", "requester", "status", "result", "created_at", "processed_at"]
    return [dict(zip(cols, r)) for r in rows]


def queue_dispatch(session_root: str, entry: dict) -> int:
    """Append to dispatch queue. Returns queue depth."""
    import os as _os

    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps({**entry, "ts": time.time()}) + "\n"
    fd = _os.open(str(queue_path), _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o644)
    try:
        _os.write(fd, row.encode())
    finally:
        _os.close(fd)
    depth = sum(1 for _ in queue_path.open())
    emit_event(
        session_root,
        "dispatch_queued",
        "dispatch-queue",
        depth=depth,
        summary=str(entry.get("summary", ""))[:200],
        agent_hint=entry.get("agent_hint"),
    )
    return depth


def read_dispatch_queue(session_root: str) -> list[dict]:
    """Read all queued dispatches."""
    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    if not queue_path.is_file():
        return []
    items = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def clear_dispatch_queue(session_root: str) -> int:
    """Clear the dispatch queue. Returns number of items cleared."""
    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    if not queue_path.is_file():
        return 0
    count = sum(1 for _ in queue_path.open())
    queue_path.unlink()
    return count


def take_dispatch_queue(session_root: str) -> list[dict]:
    """Read and clear queued dispatches as one logical operation."""
    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    if not queue_path.is_file():
        return []

    items = read_dispatch_queue(session_root)
    queue_path.unlink()
    return items


def append_idea(session_root: str, text: str, summary: str) -> None:
    """Append an idea to ideas.jsonl."""
    import os as _os

    ideas_path = Path(session_root) / ".dgov" / "ideas.jsonl"
    ideas_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "text": text, "summary": summary}
    fd = _os.open(str(ideas_path), _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o644)
    try:
        _os.write(fd, (json.dumps(entry) + "\n").encode())
    finally:
        _os.close(fd)


def _json_default(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple | set):
        return list(value)
    if hasattr(value, "value"):
        return getattr(value, "value")
    return str(value)


def _decision_kind_name(request: object) -> str:
    from dgov.decision import (
        ClarifyRequest,
        CompletionParseRequest,
        DecisionKind,
        MonitorOutputRequest,
        ReviewOutputRequest,
        RouteTaskRequest,
    )

    if isinstance(request, RouteTaskRequest):
        return DecisionKind.ROUTE_TASK.value
    if isinstance(request, MonitorOutputRequest):
        return DecisionKind.CLASSIFY_OUTPUT.value
    if isinstance(request, ReviewOutputRequest):
        return DecisionKind.REVIEW_OUTPUT.value
    if isinstance(request, CompletionParseRequest):
        return DecisionKind.PARSE_COMPLETION.value
    if isinstance(request, ClarifyRequest):
        return DecisionKind.DISAMBIGUATE.value
    raise ValueError(f"Unsupported decision request type: {type(request).__name__}")


def record_decision_audit(session_root: str, entry) -> None:  # noqa: ANN001
    """Persist a DecisionAuditEntry to SQLite."""
    from datetime import datetime, timezone

    request_json = json.dumps(entry.request, default=_json_default)
    result_json = (
        json.dumps(entry.result, default=_json_default) if entry.result is not None else None
    )
    trace_id = None
    kind = _decision_kind_name(entry.request)

    if entry.result is not None:
        trace_id = entry.result.trace_id
        if hasattr(entry.result, "kind"):
            kind = entry.result.kind.value

    if trace_id is None:
        trace_id = getattr(entry.request, "trace_id", None)

    # Extract new columns from result/request
    model_id = entry.result.model_id if entry.result is not None else None
    confidence = entry.result.confidence if entry.result is not None else None
    pane_slug = getattr(entry.request, "pane_slug", None) or getattr(entry.request, "slug", None)

    # Extract agent_id from request or result
    agent_id = getattr(entry.request, "agent_id", None)
    if agent_id is None and entry.result is not None:
        # Try to get agent_id from result (some decision records may store it)
        agent_id = getattr(entry.result, "agent_id", None)

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute(
            "INSERT INTO decision_journal "
            "(ts, kind, provider_id, trace_id, model_id, confidence, pane_slug,"
            " agent_id, request_json, result_json, error, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                kind,
                entry.provider_id,
                trace_id,
                model_id,
                confidence,
                pane_slug,
                agent_id,
                request_json,
                result_json,
                entry.error,
                entry.duration_ms,
            ),
        )
        conn.commit()

    try:
        _retry_on_lock(_do)
    except (OSError, sqlite3.OperationalError):
        logger.warning(
            "record_decision_audit(%s, %s) dropped",
            kind,
            entry.provider_id,
        )


def read_decision_journal(
    session_root: str,
    *,
    kind: str | None = None,
    pane_slug: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read decision journal rows in chronological order.

    Args:
        session_root: Path to session root directory.
        kind: Optional filter by decision kind.
        pane_slug: Optional filter by pane slug.
        limit: Optional limit on number of results (returns newest first).
    """
    conn = _get_db(session_root)
    query = (
        "SELECT ts, kind, provider_id, trace_id, model_id, confidence, pane_slug, agent_id,"
        " request_json, result_json, error, duration_ms FROM decision_journal"
    )
    params: list[object] = []
    clauses = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if pane_slug is not None:
        clauses.append("pane_slug = ?")
        params.append(pane_slug)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    if limit is not None:
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
    else:
        query += " ORDER BY id"

    rows = conn.execute(query, tuple(params)).fetchall()
    if limit is not None:
        rows = list(reversed(rows))

    items: list[dict] = []
    for (
        ts,
        row_kind,
        provider_id,
        trace_id,
        model_id,
        confidence,
        pane_slug,
        agent_id,
        request_json,
        result_json,
        error,
        duration_ms,
    ) in rows:
        item = {
            "ts": ts,
            "kind": row_kind,
            "provider_id": provider_id,
            "trace_id": trace_id,
            "model_id": model_id,
            "confidence": confidence,
            "pane_slug": pane_slug,
            "agent_id": agent_id,
            "error": error,
            "duration_ms": duration_ms,
        }
        try:
            item["request"] = json.loads(request_json)
        except (json.JSONDecodeError, TypeError):
            item["request"] = request_json
        try:
            item["result"] = json.loads(result_json) if result_json is not None else None
        except (json.JSONDecodeError, TypeError):
            item["result"] = result_json
        items.append(item)
    return items
