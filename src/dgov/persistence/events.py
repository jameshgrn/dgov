"""Event log operations — minimal governor loop version.

Structured event storage and retrieval via SQLite.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.persistence.schema import _EVENT_TYPED_COLS, VALID_EVENTS

logger = logging.getLogger(__name__)


def emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Write a structured event to the events table in state.db."""
    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")

    def _do() -> None:
        conn = _get_db(session_root)
        ts = datetime.now(timezone.utc).isoformat()

        typed = {k: str(v) for k, v in kwargs.items() if k in _EVENT_TYPED_COLS and v is not None}
        overflow = {
            k: v for k, v in kwargs.items() if k not in _EVENT_TYPED_COLS and v is not None
        }
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
    except Exception:
        logger.warning("emit_event(%s, %s) dropped — database locked", event, pane)


def read_events(
    session_root: str,
    slug: str | None = None,
    limit: int | None = None,
    after_id: int = 0,
) -> list[dict]:
    """Read events from the SQLite events table, optionally filtered by slug.

    Use after_id to poll for new events since a known position.
    """
    _typed = ", ".join(_EVENT_TYPED_COLS)
    _select = f"id, ts, event, pane, data, {_typed}"
    conn = _get_db(session_root)
    conditions: list[str] = []
    params: list[str | int] = []
    if slug is not None:
        conditions.append("pane = ?")
        params.append(slug)
    if after_id > 0:
        conditions.append("id > ?")
        params.append(after_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    # Limited queries fetch newest-first for perf, reversed below for chronological order
    order = "ORDER BY id DESC" if limit is not None else "ORDER BY id"
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"SELECT {_select} FROM events{where} {order}{limit_clause}",
        tuple(params),
    ).fetchall()
    typed_col_names = list(_EVENT_TYPED_COLS)
    events = []
    # For limited queries we fetch newest-first for performance, then reverse to keep
    # chronological (oldest-first) order at the API boundary.
    if limit is not None:
        rows = list(reversed(rows))
    for row in rows:
        row_id, ts, event, pane, data_str = row[0], row[1], row[2], row[3], row[4]
        ev: dict = {"id": row_id, "ts": ts, "event": event, "pane": pane}
        try:
            ev.update(json.loads(data_str))
        except (json.JSONDecodeError, TypeError):
            pass
        # Overlay typed columns (non-empty values win over JSON blob)
        for i, col in enumerate(typed_col_names):
            val = row[5 + i]
            if val:
                ev[col] = val
        events.append(ev)
    return events


def latest_event_id(session_root: str) -> int:
    """Return the latest event row id, or 0 if the journal is empty."""
    conn = _get_db(session_root)
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0]) if row is not None else 0


def reset_state(session_root: str) -> None:
    """Clear events and tasks tables. Called at the start of each run."""
    conn = _get_db(session_root)
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM tasks")
    conn.commit()


__all__ = [
    "emit_event",
    "latest_event_id",
    "read_events",
    "reset_state",
]
