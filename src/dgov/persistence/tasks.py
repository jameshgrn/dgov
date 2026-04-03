"""Task state operations.

CRUD operations for task records and state transitions.
SQL tables retain 'pane' names for backwards compatibility.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict

from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.persistence.schema import (
    _COMPLETION_TARGET_STATES,
    _SETTLED_TASK_STATES,
    _TASK_COLUMNS,
    _TASK_TYPED_COLS,
    TASK_STATES,
    VALID_TRANSITIONS,
    CompletionTransitionResult,
    IllegalTransitionError,
    TaskState,
    WorkerTask,
)

logger = logging.getLogger(__name__)


def _validate_state(state: str) -> str:
    """Validate and return a canonical task state. Raises ValueError for unknown states."""
    if state not in TASK_STATES:
        raise ValueError(f"Unknown task state: {state!r}. Valid: {sorted(TASK_STATES)}")
    return state


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a task dict."""
    d = dict(row)
    # Map DB column 'pane_id' to 'task_id' for internal use
    if "pane_id" in d:
        d["task_id"] = d.pop("pane_id")
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    fc = d.get("file_claims")
    if isinstance(fc, str):
        try:
            d["file_claims"] = json.loads(fc)
        except (json.JSONDecodeError, TypeError):
            d["file_claims"] = []
    # Legacy metadata JSON — merge for backward compat, but typed columns win
    metadata = d.pop("metadata", None)
    if metadata:
        try:
            legacy = json.loads(str(metadata))
            for k, v in legacy.items():
                if k not in d:
                    d[k] = v
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupt task metadata for slug=%s: %.100s", d.get("slug", "?"), metadata
            )
    return d


def _insert_task_dict(conn: sqlite3.Connection, task_dict: dict) -> None:
    """Insert a task dict into the database, separating known columns from metadata."""
    values: dict = {}
    extras: dict = {}
    for k, v in task_dict.items():
        # Map 'task_id' to DB column 'pane_id' for backwards compatibility
        db_key = "pane_id" if k == "task_id" else k
        if k in _TASK_COLUMNS:
            # Serialize complex types to JSON for DB columns that expect TEXT
            if isinstance(v, (dict, list, tuple)):
                values[db_key] = json.dumps(v, default=str)
            else:
                values[db_key] = v
        elif k in _TASK_TYPED_COLS:
            if isinstance(v, (dict, list, tuple)):
                values[db_key] = json.dumps(v, default=str)
            else:
                values[db_key] = v
        else:
            # Serialize complex types (dataclasses become dicts) to JSON
            if isinstance(v, (dict, list, tuple)):
                extras[k] = json.dumps(v, default=str)
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


def add_task(session_root: str, task: WorkerTask) -> None:
    """Add a new task to the database."""

    def _do() -> None:
        conn = _get_db(session_root)
        _insert_task_dict(conn, asdict(task))
        conn.commit()

    _retry_on_lock(_do)


def remove_task(session_root: str, slug: str) -> None:
    """Remove a task from the database, recording slug in history."""

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM panes WHERE slug = ?", (slug,))
        conn.execute(
            "INSERT OR IGNORE INTO slug_history (slug, used_at) VALUES (?, ?)",
            (slug, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()

    _retry_on_lock(_do)


def get_slug_history(session_root: str) -> set[str]:
    """Return all slugs that have ever been used (including closed/removed tasks)."""
    conn = _get_db(session_root)
    rows = conn.execute("SELECT slug FROM slug_history").fetchall()
    return {row[0] for row in rows}


def get_task(session_root: str, slug: str) -> dict | None:
    """Get a single task by slug."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
    return _row_to_dict(row) if row else None


def get_tasks(session_root: str, slugs: set[str] | list[str] | tuple[str, ...]) -> list[dict]:
    """Return task rows for the provided slugs."""
    ordered_slugs = [slug for slug in slugs if slug]
    if not ordered_slugs:
        return []
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in ordered_slugs)
    rows = conn.execute(
        f"SELECT * FROM panes WHERE slug IN ({placeholders})",
        ordered_slugs,
    ).fetchall()
    by_slug = {str(row["slug"]): _row_to_dict(row) for row in rows}
    return [by_slug[slug] for slug in ordered_slugs if slug in by_slug]


def all_tasks(session_root: str) -> list[dict]:
    """Return all tasks."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes").fetchall()
    return [_row_to_dict(row) for row in rows]


def update_task_state(session_root: str, slug: str, new_state: str, force: bool = False) -> None:
    """Update the state field of a task record.

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
                "UPDATE panes SET state = ? WHERE slug = ? AND state != ?",
                (new_state, slug, new_state),
            )
        else:
            allowed_from = [
                st for st, targets in VALID_TRANSITIONS.items() if new_state in targets
            ]
            if not allowed_from:
                row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                return False

            placeholders = ", ".join("?" * len(allowed_from))
            cur = conn.execute(
                f"UPDATE panes SET state = ? WHERE slug = ? AND state IN ({placeholders})",
                [new_state, slug, *allowed_from],
            )

            if cur.rowcount == 0:
                row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                return False

        conn.commit()
        return True

    _retry_on_lock(_do)


def settle_completion_state(
    session_root: str,
    slug: str,
    new_state: str,
    *,
    allow_abandoned: bool = False,
) -> CompletionTransitionResult:
    """Set a completion state without raising on late terminal races."""
    _validate_state(new_state)
    if new_state not in _COMPLETION_TARGET_STATES:
        raise ValueError(
            f"settle_completion_state only supports {_COMPLETION_TARGET_STATES}, got {new_state!r}"
        )

    def _do() -> CompletionTransitionResult:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row

        allow_from = list(_COMPLETION_TARGET_STATES)
        if allow_abandoned:
            allow_from.append(TaskState.ABANDONED)

        placeholders = ", ".join("?" * len(allow_from))
        cur = conn.execute(
            f"UPDATE panes SET state = ? WHERE slug = ? AND state IN ({placeholders})",
            [new_state, slug, *allow_from],
        )
        changed = cur.rowcount > 0
        conn.commit()
        return CompletionTransitionResult(changed, new_state)

    return _retry_on_lock(_do)


def settle_closed(session_root: str, slug: str) -> CompletionTransitionResult:
    """Idempotent close — safe to call multiple times."""
    return settle_completion_state(session_root, slug, TaskState.CLOSED)


def settled_tasks(session_root: str) -> list[dict]:
    """Return all settled (non-active) tasks."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" * len(_SETTLED_TASK_STATES))
    rows = conn.execute(
        f"SELECT * FROM panes WHERE state IN ({placeholders})",
        list(_SETTLED_TASK_STATES),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def active_tasks(session_root: str) -> list[dict]:
    """Return all currently active tasks."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes WHERE state = ?", (TaskState.ACTIVE,)).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_active(session_root: str) -> int:
    """Return count of currently active tasks."""
    conn = _get_db(session_root)
    row = conn.execute(
        "SELECT COUNT(*) FROM panes WHERE state = ?", (TaskState.ACTIVE,)
    ).fetchone()
    return row[0] if row else 0


def replace_all_tasks(session_root: str, tasks_list: list[dict] | dict) -> None:
    """Replace all tasks in the database with the given list.

    Intended for test setup where you need to establish a known state.
    """
    if isinstance(tasks_list, dict):
        tasks_list = tasks_list.get("tasks", tasks_list.get("panes", []))

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM panes")
        for task_dict in tasks_list:
            _insert_task_dict(conn, task_dict)
        conn.commit()

    _retry_on_lock(_do)


def set_task_metadata(session_root: str, slug: str, **kwargs: object) -> None:
    """Update typed metadata fields on a specific task."""
    if not kwargs:
        return

    def _do() -> None:
        conn = _get_db(session_root)
        typed_sets: list[str] = []
        typed_vals: list[object] = []

        for k, v in kwargs.items():
            # Map 'task_id' to DB column 'pane_id'
            db_key = "pane_id" if k == "task_id" else k
            if k in _TASK_TYPED_COLS:
                if isinstance(v, dict | list):
                    typed_sets.append(f"{db_key} = ?")
                    typed_vals.append(json.dumps(v, default=str))
                else:
                    typed_sets.append(f"{db_key} = ?")
                    typed_vals.append(v)
            else:
                raise ValueError(
                    f"set_task_metadata: unknown key {k!r} for slug={slug}. "
                    f"Allowed: {sorted(_TASK_TYPED_COLS)}"
                )

        if typed_sets:
            conn.execute(
                f"UPDATE panes SET {', '.join(typed_sets)} WHERE slug = ?",
                [*typed_vals, slug],
            )
        conn.commit()

    _retry_on_lock(_do)


def update_file_claims(session_root: str, slug: str, paths: list[str]) -> None:
    """Store file claims for a task (for later settlement)."""
    conn = _get_db(session_root)
    conn.execute(
        "UPDATE panes SET file_claims = ? WHERE slug = ?",
        (json.dumps(paths), slug),
    )
    conn.commit()
