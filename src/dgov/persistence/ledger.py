"""Persistence logic for operational ledger."""

from __future__ import annotations

import json
import logging
import time

from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.persistence.schema import LedgerEntry

logger = logging.getLogger(__name__)


def _migrate_ledger(conn) -> None:
    """Ensure ledger table has affected_paths and affected_tags columns."""
    cursor = conn.execute("PRAGMA table_info(ledger)")
    columns = {row[1] for row in cursor.fetchall()}
    if "affected_paths" not in columns:
        conn.execute("ALTER TABLE ledger ADD COLUMN affected_paths TEXT DEFAULT NULL")
    if "affected_tags" not in columns:
        conn.execute("ALTER TABLE ledger ADD COLUMN affected_tags TEXT DEFAULT NULL")


def add_ledger_entry(
    session_root: str,
    category: str,
    content: str,
    affected_paths: tuple[str, ...] = (),
    affected_tags: tuple[str, ...] = (),
) -> int:
    """Add a new entry to the ledger. Returns the entry ID."""

    def _do() -> int:
        conn = _get_db(session_root)
        _migrate_ledger(conn)
        now = time.time()
        paths_json = json.dumps(list(affected_paths)) if affected_paths else None
        tags_json = json.dumps(list(affected_tags)) if affected_tags else None
        res = conn.execute(
            "INSERT INTO ledger (category, content, created_at, affected_paths, affected_tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (category, content, now, paths_json, tags_json),
        )
        return res.lastrowid or 0

    return _retry_on_lock(_do)


def _ledger_filters(
    category: str | None,
    status: str | None,
    query: str | None,
) -> tuple[list[str], list[object]]:
    filters: list[str] = []
    params: list[object] = []
    if category:
        filters.append("category = ?")
        params.append(category)
    if status:
        filters.append("status = ?")
        params.append(status)
    if query:
        filters.append("content LIKE ?")
        params.append(f"%{query}%")
    return filters, params


def _ledger_select_sql(filters: list[str]) -> str:
    sql = (
        "SELECT id, category, content, status, created_at, resolved_at, "
        "affected_paths, affected_tags FROM ledger"
    )
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    return sql + " ORDER BY created_at DESC"


def _ledger_entry_from_row(row) -> LedgerEntry:
    return LedgerEntry(
        id=row[0],
        category=row[1],
        content=row[2],
        status=row[3],
        created_at=row[4],
        resolved_at=row[5],
        affected_paths=_decode_json_tuple(row[6], "affected_paths", row[0]),
        affected_tags=_decode_json_tuple(row[7], "affected_tags", row[0]),
    )


def _decode_json_tuple(raw: object, field: str, entry_id: object) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        decoded = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Corrupt ledger %s for entry %s: %s", field, entry_id, exc)
        return ()
    if not isinstance(decoded, list):
        logger.warning("Invalid ledger %s for entry %s: expected JSON array", field, entry_id)
        return ()
    return tuple(str(item) for item in decoded)


def list_ledger_entries(
    session_root: str,
    category: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> list[LedgerEntry]:
    """List ledger entries, optionally filtered by category, status, and keyword query."""

    def _do() -> list[LedgerEntry]:
        conn = _get_db(session_root)
        _migrate_ledger(conn)
        filters, params = _ledger_filters(category, status, query)
        cursor = conn.execute(_ledger_select_sql(filters), params)
        return [_ledger_entry_from_row(row) for row in cursor.fetchall()]

    return _retry_on_lock(_do)


def resolve_ledger_entry(session_root: str, entry_id: int) -> bool:
    """Mark a ledger entry as resolved."""

    def _do() -> bool:
        conn = _get_db(session_root)
        now = time.time()
        res = conn.execute(
            "UPDATE ledger SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (now, entry_id),
        )
        return res.rowcount > 0

    return _retry_on_lock(_do)
