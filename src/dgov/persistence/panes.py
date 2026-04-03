"""Pane state operations.

CRUD operations for pane records and state transitions.
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
    _PANE_COLUMNS,
    _PANE_TYPED_COLS,
    _SETTLED_PANE_STATES,
    PANE_STATES,
    VALID_TRANSITIONS,
    CompletionTransitionResult,
    IllegalTransitionError,
    PaneState,
    WorkerPane,
)

logger = logging.getLogger(__name__)


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a pane dict."""
    d = dict(row)
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
                "Corrupt pane metadata for slug=%s: %.100s", d.get("slug", "?"), metadata
            )
    return d


def _insert_pane_dict(conn: sqlite3.Connection, pane_dict: dict) -> None:
    """Insert a pane dict into the database, separating known columns from metadata."""
    values: dict = {}
    extras: dict = {}
    for k, v in pane_dict.items():
        if k in _PANE_COLUMNS:
            values[k] = v
        elif k in _PANE_TYPED_COLS:
            if isinstance(v, (dict, list, tuple)):
                values[k] = json.dumps(v, default=str)
            else:
                values[k] = v
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


def add_pane(session_root: str, pane: WorkerPane) -> None:
    """Add a new pane to the database."""

    def _do() -> None:
        conn = _get_db(session_root)
        _insert_pane_dict(conn, asdict(pane))
        conn.commit()

    _retry_on_lock(_do)


def remove_pane(session_root: str, slug: str) -> None:
    """Remove a pane from the database, recording slug in history."""

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
    """Return all slugs that have ever been used (including closed/removed panes)."""
    conn = _get_db(session_root)
    rows = conn.execute("SELECT slug FROM slug_history").fetchall()
    return {row[0] for row in rows}


def get_pane(session_root: str, slug: str) -> dict | None:
    """Get a single pane by slug."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
    return _row_to_dict(row) if row else None


def get_panes(session_root: str, slugs: set[str] | list[str] | tuple[str, ...]) -> list[dict]:
    """Return pane rows for the provided slugs."""
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


def all_panes(session_root: str) -> list[dict]:
    """Return all panes."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes").fetchall()
    return [_row_to_dict(row) for row in rows]


def update_pane_state(session_root: str, slug: str, new_state: str, force: bool = False) -> None:
    """Update the state field of a pane record.

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

        allowed_from = {
            state for state, targets in VALID_TRANSITIONS.items() if new_state in targets
        }
        if allow_abandoned:
            allowed_from.add(PaneState.ABANDONED)

        placeholders = ", ".join("?" * len(allowed_from))
        cur = conn.execute(
            f"UPDATE panes SET state = ? WHERE slug = ? AND state IN ({placeholders})",
            [new_state, slug, *sorted(allowed_from)],
        )

        if cur.rowcount:
            conn.commit()
            return CompletionTransitionResult(state=PaneState(new_state), changed=True)

        row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
        conn.commit()

        if row is None:
            return CompletionTransitionResult(state=PaneState(new_state), changed=False)

        current_state = row["state"]
        if current_state == new_state:
            return CompletionTransitionResult(state=current_state, changed=False)

        if current_state in _SETTLED_PANE_STATES:
            return CompletionTransitionResult(state=current_state, changed=False)

        raise IllegalTransitionError(current_state, new_state, slug)

    result = _retry_on_lock(_do)
    return result


def set_pane_metadata(session_root: str, slug: str, **kwargs: object) -> None:
    """Update typed metadata fields on a specific pane."""
    if not kwargs:
        return

    def _do() -> None:
        conn = _get_db(session_root)
        typed_sets: list[str] = []
        typed_vals: list[object] = []

        for k, v in kwargs.items():
            if k in _PANE_TYPED_COLS:
                if isinstance(v, dict | list):
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(json.dumps(v, default=str))
                else:
                    typed_sets.append(f"{k} = ?")
                    typed_vals.append(v)
            else:
                raise ValueError(
                    f"set_pane_metadata: unknown key {k!r} for slug={slug}. "
                    f"Add it to _PANE_TYPED_COLS and the panes schema first."
                )

        if typed_sets:
            typed_vals.append(slug)
            sql = f"UPDATE panes SET {', '.join(typed_sets)} WHERE slug = ?"
            conn.execute(sql, typed_vals)
        conn.commit()

    _retry_on_lock(_do)


def replace_all_panes(session_root: str, panes: list[dict] | dict) -> None:
    """Replace all panes in the database with the given list.

    Intended for test setup where you need to establish a known state.
    """
    if isinstance(panes, dict):
        panes = panes.get("panes", [])

    def _do() -> None:
        conn = _get_db(session_root)
        conn.execute("DELETE FROM panes")
        for pane_dict in panes:
            _insert_pane_dict(conn, pane_dict)
        conn.commit()

    _retry_on_lock(_do)
