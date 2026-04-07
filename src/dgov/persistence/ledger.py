"""Persistence logic for operational ledger."""

from __future__ import annotations

import time

from dgov.persistence.connection import _get_db
from dgov.persistence.schema import LedgerEntry


def add_ledger_entry(session_root: str, category: str, content: str) -> int:
    """Add a new entry to the ledger. Returns the entry ID."""
    conn = _get_db(session_root)
    now = time.time()
    res = conn.execute(
        "INSERT INTO ledger (category, content, created_at) VALUES (?, ?, ?)",
        (category, content, now),
    )
    return res.lastrowid


def list_ledger_entries(
    session_root: str,
    category: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> list[LedgerEntry]:
    """List ledger entries, optionally filtered by category, status, and keyword query."""
    conn = _get_db(session_root)
    sql = "SELECT id, category, content, status, created_at, resolved_at FROM ledger"
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
