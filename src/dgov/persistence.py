"""State file management and event journal.

Manages .dgov/state.db (pane records via SQLite WAL) and .dgov/events.jsonl (event log).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dgov import tmux

logger = logging.getLogger(__name__)

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
        "checkpoint_created",
        "review_pass",
        "review_fail",
        "experiment_started",
        "experiment_accepted",
        "experiment_rejected",
        "review_fix_started",
        "review_fix_finding",
        "review_fix_completed",
    }
)


def _emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Append a structured event to .dgov/events.jsonl."""
    from datetime import datetime, timezone

    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")
    events_path = Path(session_root) / _STATE_DIR / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "pane": pane,
        **kwargs,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


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

_STATE_DIR = ".dgov"
_PROTECTED_FILES = {"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md", ".napkin.md"}
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


def _state_path(session_root: str) -> Path:
    return Path(session_root) / _STATE_DIR / _STATE_FILE


def _get_db(session_root: str) -> sqlite3.Connection:
    """Open a SQLite connection, creating/migrating the DB on first access."""
    db_path = _state_path(session_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()

    # Migrate from JSON if state.json exists
    json_path = db_path.parent / "state.json"
    if json_path.exists():
        try:
            with open(json_path) as f:
                old_state = json.load(f)
            panes = old_state.get("panes", [])
            for pane_dict in panes:
                _insert_pane_dict(conn, pane_dict)
            conn.commit()
            json_path.unlink()
            logger.info("Migrated %d panes from state.json to state.db", len(panes))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to migrate state.json: %s", e)

    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a pane dict, merging any metadata extras."""
    d = dict(row)
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    metadata = d.pop("metadata", None)
    if metadata:
        try:
            d.update(json.loads(metadata))
        except (json.JSONDecodeError, TypeError):
            pass
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


def _read_state(session_root: str) -> dict:
    conn = _get_db(session_root)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM panes").fetchall()
        return {"panes": [_row_to_dict(row) for row in rows]}
    finally:
        conn.close()


def _write_state(session_root: str, state: dict) -> None:
    conn = _get_db(session_root)
    try:
        conn.execute("DELETE FROM panes")
        for pane_dict in state.get("panes", []):
            _insert_pane_dict(conn, pane_dict)
        conn.commit()
    finally:
        conn.close()


def _add_pane(session_root: str, pane: WorkerPane) -> None:
    conn = _get_db(session_root)
    try:
        _insert_pane_dict(conn, asdict(pane))
        conn.commit()
    finally:
        conn.close()


def _remove_pane(session_root: str, slug: str) -> None:
    conn = _get_db(session_root)
    try:
        conn.execute("DELETE FROM panes WHERE slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


def _get_pane(session_root: str, slug: str) -> dict | None:
    conn = _get_db(session_root)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _all_panes(session_root: str) -> list[dict]:
    conn = _get_db(session_root)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM panes").fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def _update_pane_state(session_root: str, slug: str, new_state: str) -> None:
    """Update the state field of a pane record."""
    _validate_state(new_state)
    conn = _get_db(session_root)
    try:
        conn.execute("UPDATE panes SET state = ? WHERE slug = ?", (new_state, slug))
        conn.commit()
    finally:
        conn.close()

    # Update tmux pane title to reflect new status
    pane = _get_pane(session_root, slug)
    if pane:
        pane_id = pane.get("pane_id", "")
        agent = pane.get("agent", "")
        if pane_id:
            tmux.update_pane_status(pane_id, agent, slug, new_state)
