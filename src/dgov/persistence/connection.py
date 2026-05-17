"""Connection management for persistence layer — minimal governor loop version.

Handles SQLite connection caching, WAL mode, and schema initialization.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

from dgov.persistence.schema import (
    _CREATE_DISPATCH_RUNS_TABLE_SQL,
    _CREATE_EVENTS_TABLE_SQL,
    _CREATE_TABLE_SQL,
    state_path,
)

# -- Connection cache (per db_path + thread) --

_conn_cache: dict[tuple[str, int], sqlite3.Connection] = {}
_conn_lock = threading.Lock()

# WAL busy_timeout=10s handles most lock contention. These retries are a
# last-resort layer — cap them tightly so we fail fast instead of hanging.
_LOCK_RETRIES = 5
_LOCK_BACKOFF_S = 0.25
_CONNECT_TIMEOUT_S = 10.0


def _open_db_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=_CONNECT_TIMEOUT_S)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")

        # Schema initialization (idempotent)
        from dgov.persistence.sql import _CREATE_LEDGER_TABLE_SQL, _CREATE_SLUG_HISTORY_TABLE_SQL

        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_EVENTS_TABLE_SQL)
        conn.execute(_CREATE_DISPATCH_RUNS_TABLE_SQL)
        conn.execute(_CREATE_SLUG_HISTORY_TABLE_SQL)
        conn.execute(_CREATE_LEDGER_TABLE_SQL)

        _migrate_schema(conn)
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise
    return conn


def _get_db(session_root: str) -> sqlite3.Connection:
    """Return a cached SQLite connection for this (db_path, thread).

    First call per thread creates the connection, sets WAL mode,
    busy_timeout, and runs CREATE TABLE. Subsequent calls return
    the cached connection.
    """
    db_path = str(state_path(session_root))
    key = (db_path, threading.get_ident())

    with _conn_lock:
        conn = _conn_cache.get(key)
        if conn is not None:
            return conn

    # Outside the lock — only one thread will ever hit this for a given key.
    conn = _retry_on_lock(_open_db_connection, db_path)

    with _conn_lock:
        # Another racer may have inserted; prefer the first one.
        existing = _conn_cache.get(key)
        if existing is not None:
            conn.close()
            return existing
        _conn_cache[key] = conn
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add missing columns to existing tables (safe to run multiple times)."""
    # 1. Migrate 'events' table
    cursor = conn.execute("PRAGMA table_info(events)")
    existing_events_cols = {row[1] for row in cursor.fetchall()}

    new_events_cols = {
        "task_slug": "TEXT DEFAULT NULL",
        "plan_name": "TEXT DEFAULT NULL",
        "action": "TEXT DEFAULT NULL",
        "run_source": "TEXT DEFAULT NULL",
    }
    for col, dtype in new_events_cols.items():
        if col not in existing_events_cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {dtype}")

    cursor = conn.execute("PRAGMA table_info(dispatch_runs)")
    existing_dispatch_cols = {row[1] for row in cursor.fetchall()}

    if "run_source" not in existing_dispatch_cols:
        conn.execute(
            "ALTER TABLE dispatch_runs ADD COLUMN run_source TEXT NOT NULL DEFAULT 'manual'"
        )

    # 2. Migrate 'tasks' table
    cursor = conn.execute("PRAGMA table_info(tasks)")
    existing_tasks_cols = {row[1] for row in cursor.fetchall()}

    if "plan_name" not in existing_tasks_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN plan_name TEXT DEFAULT NULL")

    conn.commit()


def _retry_on_lock(fn, *args, **kwargs) -> Any:
    """Call *fn* with retries on 'database is locked' errors."""
    import logging
    import time

    logger = logging.getLogger(__name__)

    for attempt in range(_LOCK_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == _LOCK_RETRIES - 1:
                raise
            logger.debug("database locked, retry %d/%d", attempt + 1, _LOCK_RETRIES)
            time.sleep(_LOCK_BACKOFF_S)
    return None  # unreachable, but keeps type checkers happy


def clear_connection_cache() -> None:
    """Clear the connection cache. Useful for testing."""
    global _conn_cache
    with _conn_lock:
        for conn in _conn_cache.values():
            with contextlib.suppress(Exception):
                conn.close()
        _conn_cache.clear()


__all__ = [
    "_conn_cache",
    "_conn_lock",
    "_get_db",
    "_retry_on_lock",
    "clear_connection_cache",
]
