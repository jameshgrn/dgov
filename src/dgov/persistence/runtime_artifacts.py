"""Runtime artifact record operations.

This layer stores best-effort execution metadata such as worktree paths,
branch names, and a cached per-task artifact state. Lifecycle truth lives in
the event log; these rows exist only for operational bookkeeping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict

from dgov.persistence._tasks_helpers import (
    _insert_task_dict,
    _row_to_dict,
    _validate_state,
)
from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.persistence.schema import (
    _TASK_TYPED_COLS,
    VALID_TRANSITIONS,
    IllegalTransitionError,
    TaskState,
    WorkerTask,
)

logger = logging.getLogger(__name__)


def record_runtime_artifact(session_root: str, task: WorkerTask) -> None:
    """Insert or replace a runtime artifact row."""

    def _do() -> None:
        conn = _get_db(session_root)
        _insert_task_dict(conn, asdict(task))
        conn.commit()

    _retry_on_lock(_do)


def remove_runtime_artifact(session_root: str, slug: str) -> None:
    """Remove a runtime artifact row, recording slug in history."""

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM tasks WHERE slug = ?", (slug,))
        conn.execute(
            "INSERT OR IGNORE INTO slug_history (slug, used_at) VALUES (?, ?)",
            (slug, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()

    import time

    _retry_on_lock(_do)


def get_slug_history(session_root: str) -> set[str]:
    """Return all slugs that have ever been used (including closed/removed tasks)."""
    conn = _get_db(session_root)
    rows = conn.execute("SELECT slug FROM slug_history").fetchall()
    return {row[0] for row in rows}


def get_runtime_artifact(session_root: str, slug: str) -> dict | None:
    """Get a single runtime artifact row by slug."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE slug = ?", (slug,)).fetchone()
    return _row_to_dict(row) if row else None


def get_runtime_artifacts(
    session_root: str, slugs: set[str] | list[str] | tuple[str, ...]
) -> list[dict]:
    """Return runtime artifact rows for the provided slugs."""
    ordered_slugs = [slug for slug in slugs if slug]
    if not ordered_slugs:
        return []
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in ordered_slugs)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE slug IN ({placeholders})",
        ordered_slugs,
    ).fetchall()
    by_slug = {str(row["slug"]): _row_to_dict(row) for row in rows}
    return [by_slug[slug] for slug in ordered_slugs if slug in by_slug]


def list_runtime_artifacts(session_root: str) -> list[dict]:
    """Return all runtime artifact rows."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    return [_row_to_dict(row) for row in rows]


def update_runtime_artifact_state(
    session_root: str, slug: str, new_state: str, force: bool = False
) -> None:
    """Update the cached state field of a runtime artifact row.

    Enforces VALID_TRANSITIONS unless *force* is True.
    Same-state transitions are no-ops.
    Uses an atomic UPDATE ... WHERE to avoid read-check-write races.
    Raises IllegalTransitionError for disallowed transitions.
    """
    _validate_state(new_state)

    def _do() -> bool:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row

        if force:
            cur = conn.execute(
                "UPDATE tasks SET state = ? WHERE slug = ? AND state != ?",
                (new_state, slug, new_state),
            )
        else:
            allowed_from = [
                st for st, targets in VALID_TRANSITIONS.items() if new_state in targets
            ]
            if not allowed_from:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE slug = ?",
                    (slug,),
                ).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                return False

            placeholders = ", ".join("?" * len(allowed_from))
            cur = conn.execute(
                f"UPDATE tasks SET state = ? WHERE slug = ? AND state IN ({placeholders})",
                [new_state, slug, *allowed_from],
            )

            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT state FROM tasks WHERE slug = ?",
                    (slug,),
                ).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                return False

        conn.commit()
        return True

    _retry_on_lock(_do)


def prune_runtime_artifact_history(session_root: str) -> int:
    """Delete abandoned and closed runtime artifact rows. Returns count removed."""
    _HISTORICAL = (TaskState.ABANDONED.value, TaskState.CLOSED.value)

    def _do() -> int:
        conn = _get_db(session_root)
        placeholders = ", ".join("?" * len(_HISTORICAL))
        cur = conn.execute(
            f"DELETE FROM tasks WHERE state IN ({placeholders})",
            _HISTORICAL,
        )
        conn.commit()
        return cur.rowcount

    return _retry_on_lock(_do)


def replace_runtime_artifacts(session_root: str, tasks_list: list[dict] | dict) -> None:
    """Replace all runtime artifact rows in the database with the given list.

    Intended for test setup where you need to establish a known state.
    """
    if isinstance(tasks_list, dict):
        tasks_list = tasks_list.get("tasks", [])

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM tasks")
        for task_dict in tasks_list:
            _insert_task_dict(conn, task_dict)
        conn.commit()

    _retry_on_lock(_do)


def set_runtime_artifact_metadata(session_root: str, slug: str, **kwargs: object) -> None:
    """Update typed metadata fields on a specific runtime artifact row."""
    if not kwargs:
        return

    def _do() -> None:
        conn = _get_db(session_root)
        typed_sets: list[str] = []
        typed_vals: list[object] = []

        for k, v in kwargs.items():
            if k in _TASK_TYPED_COLS:
                if isinstance(v, dict | list):
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(json.dumps(v, default=str))
                else:
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(v)
            else:
                raise ValueError(
                    f"set_runtime_artifact_metadata: unknown key {k!r} for slug={slug}. "
                    f"Allowed: {sorted(_TASK_TYPED_COLS)}"
                )

        if typed_sets:
            conn.execute(
                f"UPDATE tasks SET {', '.join(typed_sets)} WHERE slug = ?",
                [*typed_vals, slug],
            )
        conn.commit()

    _retry_on_lock(_do)
