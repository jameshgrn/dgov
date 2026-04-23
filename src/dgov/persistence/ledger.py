"""Persistence logic for operational ledger."""

from __future__ import annotations

import json
import time

from dgov.persistence.connection import _get_db
from dgov.persistence.schema import LedgerEntry


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


def list_ledger_entries(
    session_root: str,
    category: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> list[LedgerEntry]:
    """List ledger entries, optionally filtered by category, status, and keyword query."""
    conn = _get_db(session_root)
    _migrate_ledger(conn)
    sql = (
        "SELECT id, category, content, status, created_at, resolved_at, "
        "affected_paths, affected_tags FROM ledger"
    )
    params = []
    filters = []

    if category:
        filters.append("category = ?")
        params.append(category)
    if status:
        filters.append("status = ?")
        params.append(status)
    if query:
        filters.append("content LIKE ?")
        params.append(f"%{query}%")

    if filters:
        sql += " WHERE " + " AND ".join(filters)

    sql += " ORDER BY created_at DESC"

    cursor = conn.execute(sql, params)
    return [
        LedgerEntry(
            id=row[0],
            category=row[1],
            content=row[2],
            status=row[3],
            created_at=row[4],
            resolved_at=row[5],
            affected_paths=tuple(json.loads(row[6])) if row[6] else (),
            affected_tags=tuple(json.loads(row[7])) if row[7] else (),
        )
        for row in cursor.fetchall()
    ]


def resolve_ledger_entry(session_root: str, entry_id: int) -> bool:
    """Mark a ledger entry as resolved."""
    conn = _get_db(session_root)
    now = time.time()
    res = conn.execute(
        "UPDATE ledger SET status = 'resolved', resolved_at = ? WHERE id = ?",
        (now, entry_id),
    )
    return res.rowcount > 0
