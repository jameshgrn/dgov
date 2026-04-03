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
    except Exception:
        logger.warning("emit_event(%s, %s) dropped — database locked", event, pane)


def read_events(
    session_root: str,
    slug: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read events from the SQLite events table, optionally filtered by slug."""
    _typed = ", ".join(_EVENT_TYPED_COLS)
    _select = f"ts, event, pane, data, {_typed}"
    conn = _get_db(session_root)
    where = ""
    params: list[str | int] = []
    if slug is not None:
        where = " WHERE pane = ?"
        params.append(slug)
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
    """Simple polling-based wait for new events after *after_id*."""
    import time

    conn = _get_db(session_root)
    pane_filters = tuple(dict.fromkeys(p for p in panes if p))
    event_filters = tuple(dict.fromkeys(e for e in event_types if e))

    typed_col_names = list(_EVENT_TYPED_COLS)
    typed_select = ", ".join(typed_col_names)
    query = [f"SELECT id, ts, event, pane, data, {typed_select} FROM events WHERE id > ?"]
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
            for row in rows:
                event_id, ts, event, pane, data_str = row[:5]
                ev = {"id": event_id, "ts": ts, "event": event, "pane": pane}
                try:
                    ev.update(json.loads(data_str))
                except (json.JSONDecodeError, TypeError):
                    pass
                for i, col in enumerate(typed_col_names):
                    val = row[5 + i]
                    if val:
                        ev[col] = val
                events.append(ev)
            return events

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(0.1)  # Simple polling fallback


__all__ = [
    "emit_event",
    "latest_event_id",
    "read_events",
    "wait_for_events",
]
