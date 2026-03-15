"""State file management and event journal.

Manages .dgov/state.db (pane records and event log via SQLite WAL).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# -- Connection cache (per db_path + thread) --

_conn_cache: dict[tuple[str, int], sqlite3.Connection] = {}
_conn_lock = threading.Lock()

# -- Event log --

VALID_EVENTS = frozenset(
    {
        "pane_created",
        "pane_done",
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
        "checkpoint_created",
        "review_pass",
        "review_fail",
        "experiment_started",
        "experiment_accepted",
        "experiment_rejected",
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
    }
)


def emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Write a structured event to the events table in state.db."""
    from datetime import datetime, timezone

    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")

    def _do() -> None:
        conn = _get_db(session_root)
        ts = datetime.now(timezone.utc).isoformat()
        data = json.dumps(kwargs, default=str) if kwargs else "{}"
        conn.execute(
            "INSERT INTO events (ts, event, pane, data) VALUES (?, ?, ?, ?)",
            (ts, event, pane, data),
        )
        conn.commit()

    _retry_on_lock(_do)


def read_events(session_root: str, slug: str | None = None) -> list[dict]:
    """Read events from the SQLite events table, optionally filtered by slug."""
    conn = _get_db(session_root)
    if slug is not None:
        rows = conn.execute(
            "SELECT ts, event, pane, data FROM events WHERE pane = ? ORDER BY id",
            (slug,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT ts, event, pane, data FROM events ORDER BY id").fetchall()
    events = []
    for ts, event, pane, data_str in rows:
        ev = {"ts": ts, "event": event, "pane": pane}
        try:
            ev.update(json.loads(data_str))
        except (json.JSONDecodeError, TypeError):
            pass
        events.append(ev)
    return events


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


# Transition table: 12 states, enforced in update_pane_state
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset(
        {"done", "failed", "abandoned", "timed_out", "closed", "escalated", "superseded"}
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
    state: str = "active"

    def __post_init__(self) -> None:
        _validate_state(self.state)


# -- State DB helpers --

STATE_DIR = ".dgov"
PROTECTED_FILES = {"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md", ".napkin.md"}
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
        "state",
    }
)

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
    state TEXT,
    metadata TEXT
)"""

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    pane TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}')
"""


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
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_EVENTS_TABLE_SQL)
    conn.commit()

    with _conn_lock:
        # Another racer may have inserted; prefer the first one.
        existing = _conn_cache.get(key)
        if existing is not None:
            conn.close()
            return existing
        _conn_cache[key] = conn
    return conn


def _close_cached_connections() -> None:
    """Close and remove all cached connections. For test cleanup."""
    with _conn_lock:
        for conn in _conn_cache.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn_cache.clear()


_LOCK_RETRIES = 5
_LOCK_BACKOFF_S = 0.2


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
    """Convert a SQLite row to a pane dict, merging any metadata extras."""
    d = dict(row)
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    metadata = d.pop("metadata", None)
    if metadata:
        try:
            d.update(json.loads(str(metadata)))
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
        conn.execute("DELETE FROM panes WHERE slug = ?", (slug,))
        conn.commit()

    _retry_on_lock(_do)


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

    # Update pane title to reflect new status.
    pane = get_pane(session_root, slug)
    if pane:
        pane_id = pane.get("pane_id", "")
        agent = pane.get("agent", "")
        if pane_id:
            from dgov.backend import get_backend

            icon = {
                "active": "\u23f3",
                "done": "\u2713",
                "failed": "\u2717",
                "timed_out": "\u23f0",
            }.get(new_state, "?")
            try:
                get_backend().set_title(pane_id, f"[{agent}] {slug} {icon}")
            except (RuntimeError, OSError):
                pass  # pane may already be dead


def set_pane_metadata(session_root: str, slug: str, **kwargs: object) -> None:
    """Update metadata fields on a specific pane.

    Stores extra fields (like ``max_retries``, ``retried_from``, ``superseded_by``)
    in the ``metadata`` JSON column.
    """

    def _do() -> None:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return
        d = _row_to_dict(row)
        for k, v in kwargs.items():
            d[k] = v
        _insert_pane_dict(conn, d)
        conn.commit()

    _retry_on_lock(_do)


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
