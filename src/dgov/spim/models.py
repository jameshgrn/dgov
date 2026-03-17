"""SQLite-backed persistence for SPIM agents, claims, locks, deltas, and events."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AGENT_STATES = frozenset({"idle", "watching", "proposing", "acting", "blocked", "done"})
CLAIM_STATUSES = frozenset({"pending", "accepted", "rejected", "blocked", "applied"})

_CONN_CACHE: dict[tuple[str, int], sqlite3.Connection] = {}
_CONN_LOCK = threading.Lock()
_LOCK_RETRIES = 20
_LOCK_BACKOFF_S = 0.5

_CREATE_AGENTS_SQL = """\
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    focus_region TEXT NOT NULL,
    spawned_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)"""

_CREATE_EVENTS_SQL = """\
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id INTEGER,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (agent_id) REFERENCES agents(id)
)"""

_CREATE_CLAIMS_SQL = """\
CREATE TABLE IF NOT EXISTS claims (
    claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL,
    region TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
)"""

_CREATE_LOCKS_SQL = """\
CREATE TABLE IF NOT EXISTS locks (
    region TEXT PRIMARY KEY,
    held_by INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (held_by) REFERENCES agents(id)
)"""

_CREATE_DELTAS_SQL = """\
CREATE TABLE IF NOT EXISTS deltas (
    delta_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    patch_json TEXT NOT NULL,
    applied_at TEXT,
    reverted_at TEXT,
    FOREIGN KEY (claim_id) REFERENCES claims(claim_id)
)"""

_CREATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_spim_claims_status ON claims(status)",
    "CREATE INDEX IF NOT EXISTS idx_spim_claims_agent ON claims(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_spim_deltas_claim ON deltas(claim_id)",
    "CREATE INDEX IF NOT EXISTS idx_spim_events_agent ON events(agent_id)",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def add_ttl(base: datetime, ttl_seconds: float) -> str:
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
    return isoformat_utc(base + timedelta(seconds=ttl_seconds))


def _normalize_db_path(db_path: str | Path) -> str:
    return str(Path(db_path).expanduser().resolve())


def ensure_schema(db_path: str | Path) -> sqlite3.Connection:
    db_file = _normalize_db_path(db_path)
    key = (db_file, threading.get_ident())

    with _CONN_LOCK:
        conn = _CONN_CACHE.get(key)
        if conn is not None:
            return conn

    Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_CREATE_AGENTS_SQL)
    conn.execute(_CREATE_EVENTS_SQL)
    conn.execute(_CREATE_CLAIMS_SQL)
    conn.execute(_CREATE_LOCKS_SQL)
    conn.execute(_CREATE_DELTAS_SQL)
    for statement in _CREATE_INDEXES_SQL:
        conn.execute(statement)
    conn.commit()

    with _CONN_LOCK:
        existing = _CONN_CACHE.get(key)
        if existing is not None:
            conn.close()
            return existing
        _CONN_CACHE[key] = conn
    return conn


def close_cached_connections() -> None:
    with _CONN_LOCK:
        for conn in _CONN_CACHE.values():
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("error while closing SPIM SQLite connection", exc_info=True)
        _CONN_CACHE.clear()


def _retry_on_lock(fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    for attempt in range(_LOCK_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == _LOCK_RETRIES - 1:
                raise
            logger.debug("database locked, retry %d/%d", attempt + 1, _LOCK_RETRIES)
            time.sleep(_LOCK_BACKOFF_S * (attempt + 1))
    return None


def _validate_agent_status(status: str) -> str:
    if status not in AGENT_STATES:
        raise ValueError(f"Unknown agent status: {status!r}. Valid: {sorted(AGENT_STATES)}")
    return status


def _validate_claim_status(status: str) -> str:
    if status not in CLAIM_STATUSES:
        raise ValueError(f"Unknown claim status: {status!r}. Valid: {sorted(CLAIM_STATUSES)}")
    return status


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, default=str, sort_keys=True)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for json_key in ("payload_json", "patch_json"):
        raw_value = data.get(json_key)
        if isinstance(raw_value, str):
            data[json_key] = json.loads(raw_value)
    return data


def _row_to_required_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_to_dict(row)
    if data is None:
        raise RuntimeError("Expected SQLite row but received None")
    return data


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return int(cursor.lastrowid)


def create_agent(
    db_path: str | Path,
    role: str,
    focus_region: str,
    *,
    status: str = "idle",
    spawned_at: str | None = None,
    expires_at: str | None = None,
) -> int:
    _validate_agent_status(status)

    def _do() -> int:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            """
            INSERT INTO agents (role, status, focus_region, spawned_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                role,
                status,
                focus_region,
                spawned_at or isoformat_utc(),
                expires_at or isoformat_utc(),
            ),
        )
        conn.commit()
        return _lastrowid(cursor)

    return _retry_on_lock(_do)


def get_agent(db_path: str | Path, agent_id: int) -> dict[str, Any] | None:
    conn = ensure_schema(db_path)
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return _row_to_dict(row)


def list_agents(
    db_path: str | Path,
    *,
    status: str | None = None,
    region: str | None = None,
) -> list[dict[str, Any]]:
    conn = ensure_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []

    if status is not None:
        _validate_agent_status(status)
        clauses.append("status = ?")
        params.append(status)
    if region is not None:
        clauses.append("focus_region = ?")
        params.append(region)

    query = "SELECT * FROM agents"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_required_dict(row) for row in rows]


def update_agent(db_path: str | Path, agent_id: int, **fields: Any) -> None:
    if not fields:
        return

    allowed = {"role", "status", "focus_region", "spawned_at", "expires_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown agent fields: {sorted(unknown)}")
    if "status" in fields:
        fields["status"] = _validate_agent_status(str(fields["status"]))

    def _do() -> None:
        conn = ensure_schema(db_path)
        assignments = ", ".join(f"{field} = ?" for field in fields)
        cursor = conn.execute(
            f"UPDATE agents SET {assignments} WHERE id = ?",
            [*fields.values(), agent_id],
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        conn.commit()

    _retry_on_lock(_do)


def expire_agents(db_path: str | Path, *, now: datetime | None = None) -> list[int]:
    cutoff = isoformat_utc(now)

    def _do() -> list[int]:
        conn = ensure_schema(db_path)
        rows = conn.execute(
            "SELECT id FROM agents WHERE expires_at <= ? AND status != 'done'",
            (cutoff,),
        ).fetchall()
        expired_ids = [int(row["id"]) for row in rows]
        if expired_ids:
            placeholders = ", ".join("?" for _ in expired_ids)
            conn.execute(
                f"UPDATE agents SET status = 'done' WHERE id IN ({placeholders})",
                expired_ids,
            )
            conn.commit()
        return expired_ids

    return _retry_on_lock(_do)


def record_event(
    db_path: str | Path,
    agent_id: int | None,
    event_type: str,
    payload: Any | None = None,
    *,
    ts: str | None = None,
) -> int:
    def _do() -> int:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            """
            INSERT INTO events (ts, agent_id, type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (ts or isoformat_utc(), agent_id, event_type, _json_dumps(payload)),
        )
        conn.commit()
        return _lastrowid(cursor)

    return _retry_on_lock(_do)


def list_events(
    db_path: str | Path,
    *,
    agent_id: int | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    conn = ensure_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []

    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if event_type is not None:
        clauses.append("type = ?")
        params.append(event_type)

    query = "SELECT ts, agent_id, type, payload_json FROM events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_required_dict(row) for row in rows]


def create_claim(
    db_path: str | Path,
    agent_id: int,
    region: str,
    kind: str,
    confidence: float,
    *,
    status: str = "pending",
) -> int:
    _validate_claim_status(status)

    def _do() -> int:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            """
            INSERT INTO claims (agent_id, region, kind, confidence, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent_id, region, kind, confidence, status),
        )
        conn.commit()
        return _lastrowid(cursor)

    return _retry_on_lock(_do)


def get_claim(db_path: str | Path, claim_id: int) -> dict[str, Any] | None:
    conn = ensure_schema(db_path)
    row = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    return _row_to_dict(row)


def list_claims(
    db_path: str | Path,
    *,
    status: str | None = None,
    agent_id: int | None = None,
    region: str | None = None,
) -> list[dict[str, Any]]:
    conn = ensure_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []

    if status is not None:
        _validate_claim_status(status)
        clauses.append("status = ?")
        params.append(status)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if region is not None:
        clauses.append("region = ?")
        params.append(region)

    query = "SELECT * FROM claims"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY claim_id"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_required_dict(row) for row in rows]


def update_claim_status(db_path: str | Path, claim_id: int, status: str) -> None:
    _validate_claim_status(status)

    def _do() -> None:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            "UPDATE claims SET status = ? WHERE claim_id = ?",
            (status, claim_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown claim_id: {claim_id}")
        conn.commit()

    _retry_on_lock(_do)


def create_delta(
    db_path: str | Path,
    claim_id: int,
    patch: Any,
    *,
    applied_at: str | None = None,
    reverted_at: str | None = None,
) -> int:
    def _do() -> int:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            """
            INSERT INTO deltas (claim_id, patch_json, applied_at, reverted_at)
            VALUES (?, ?, ?, ?)
            """,
            (claim_id, _json_dumps(patch), applied_at, reverted_at),
        )
        conn.commit()
        return _lastrowid(cursor)

    return _retry_on_lock(_do)


def get_delta(db_path: str | Path, delta_id: int) -> dict[str, Any] | None:
    conn = ensure_schema(db_path)
    row = conn.execute("SELECT * FROM deltas WHERE delta_id = ?", (delta_id,)).fetchone()
    return _row_to_dict(row)


def get_delta_for_claim(db_path: str | Path, claim_id: int) -> dict[str, Any] | None:
    conn = ensure_schema(db_path)
    row = conn.execute(
        "SELECT * FROM deltas WHERE claim_id = ? ORDER BY delta_id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    return _row_to_dict(row)


def list_deltas(
    db_path: str | Path,
    *,
    claim_id: int | None = None,
    applied: bool | None = None,
) -> list[dict[str, Any]]:
    conn = ensure_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []

    if claim_id is not None:
        clauses.append("claim_id = ?")
        params.append(claim_id)
    if applied is True:
        clauses.append("applied_at IS NOT NULL")
    elif applied is False:
        clauses.append("applied_at IS NULL")

    query = "SELECT * FROM deltas"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY delta_id"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_required_dict(row) for row in rows]


def mark_delta_applied(
    db_path: str | Path,
    delta_id: int,
    *,
    applied_at: str | None = None,
) -> None:
    def _do() -> None:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            "UPDATE deltas SET applied_at = ? WHERE delta_id = ?",
            (applied_at or isoformat_utc(), delta_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown delta_id: {delta_id}")
        conn.commit()

    _retry_on_lock(_do)


def mark_delta_reverted(
    db_path: str | Path,
    delta_id: int,
    *,
    reverted_at: str | None = None,
) -> None:
    def _do() -> None:
        conn = ensure_schema(db_path)
        cursor = conn.execute(
            "UPDATE deltas SET reverted_at = ? WHERE delta_id = ?",
            (reverted_at or isoformat_utc(), delta_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown delta_id: {delta_id}")
        conn.commit()

    _retry_on_lock(_do)


def get_lock(db_path: str | Path, region: str) -> dict[str, Any] | None:
    conn = ensure_schema(db_path)
    row = conn.execute("SELECT * FROM locks WHERE region = ?", (region,)).fetchone()
    return _row_to_dict(row)


def delete_lock(db_path: str | Path, region: str, agent_id: int | None = None) -> bool:
    def _do() -> bool:
        conn = ensure_schema(db_path)
        if agent_id is None:
            cursor = conn.execute("DELETE FROM locks WHERE region = ?", (region,))
        else:
            cursor = conn.execute(
                "DELETE FROM locks WHERE region = ? AND held_by = ?",
                (region, agent_id),
            )
        conn.commit()
        return cursor.rowcount > 0

    return _retry_on_lock(_do)
