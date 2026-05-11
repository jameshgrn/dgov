"""Task database helpers — serialization and validation utilities."""

from __future__ import annotations

import json
import logging
import sqlite3

from dgov.persistence.schema import (
    _TASK_COLUMNS,
    _TASK_TYPED_COLS,
    TASK_STATES,
)

logger = logging.getLogger(__name__)

_NON_PERSISTED_TASK_FIELDS = frozenset({"prompt", "file_claims", "commit_message"})
_PERSISTED_TASK_FIELDS = _TASK_COLUMNS | _TASK_TYPED_COLS


def _validate_state(state: str) -> str:
    """Validate and return a canonical task state. Raises ValueError for unknown states."""
    if state not in TASK_STATES:
        raise ValueError(f"Unknown task state: {state!r}. Valid: {sorted(TASK_STATES)}")
    return state


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a task dict."""
    d = dict(row)
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    # Legacy metadata JSON — merge for backward compat, but typed columns win
    metadata = d.pop("metadata", None)
    if metadata:
        try:
            legacy = json.loads(str(metadata))
            for k, v in legacy.items():
                if k not in d and k not in _NON_PERSISTED_TASK_FIELDS:
                    d[k] = v
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupt task metadata for slug=%s: %.100s", d.get("slug", "?"), metadata
            )
    for field in _NON_PERSISTED_TASK_FIELDS:
        d.pop(field, None)
    return d


def _insert_task_dict(conn: sqlite3.Connection, task_dict: dict) -> None:
    """Insert a task dict into the database, separating known columns from metadata."""
    values, extras = _split_task_values(task_dict)
    _normalize_task_values(values, extras)
    _insert_task_values(conn, values)


def _split_task_values(task_dict: dict) -> tuple[dict, dict]:
    values: dict = {}
    extras: dict = {}
    for k, v in task_dict.items():
        if k in _NON_PERSISTED_TASK_FIELDS:
            continue
        if k in _PERSISTED_TASK_FIELDS:
            values[k] = _db_task_value(v)
        else:
            extras[k] = _db_task_value(v)
    return values, extras


def _db_task_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return value


def _normalize_task_values(values: dict, extras: dict) -> None:
    if "owns_worktree" in values and isinstance(values["owns_worktree"], bool):
        values["owns_worktree"] = int(values["owns_worktree"])
    values["metadata"] = json.dumps(extras) if extras else None


def _insert_task_values(conn: sqlite3.Connection, values: dict) -> None:
    cols = ", ".join(values.keys())
    placeholders = ", ".join("?" * len(values))
    conn.execute(
        f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({placeholders})",
        list(values.values()),
    )
