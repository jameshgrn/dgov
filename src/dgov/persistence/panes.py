"""Pane state operations.

CRUD operations for pane records and state transitions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.persistence.schema import (
    CIRCUIT_BREAKER_THRESHOLD,
    CompletionTransitionResult,
    IllegalTransitionError,
    PANE_STATES,
    PaneState,
    STATE_DIR,
    VALID_TRANSITIONS,
    WorkerPane,
    _COMPLETION_TARGET_STATES,
    _PANE_COLUMNS,
    _PANE_TYPED_COLS,
    _SETTLED_PANE_STATES,
)

logger = logging.getLogger(__name__)


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a pane dict, merging any legacy metadata extras."""
    d = dict(row)
    if d.get("owns_worktree") is not None:
        d["owns_worktree"] = bool(d["owns_worktree"])
    # Deserial file_claims from JSON string back to list/tuple
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
            # Only fill keys not already present as typed columns
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


def remove_pane(session_root: str, slug: str, crash_log: str = "") -> None:
    """Remove a pane from the database, archiving it first."""

    def _do() -> None:
        conn = _get_db(session_root)
        # Archive before delete
        row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
        if row:
            cols = [d[0] for d in conn.execute("SELECT * FROM panes LIMIT 0").description]
            pane_dict = dict(zip(cols, row))
            try:
                from dgov.spans import archive_pane

                archive_pane(session_root, pane_dict, crash_log=crash_log)
            except Exception:
                pass  # archive failure must not block deletion
        conn.execute("DELETE FROM panes WHERE slug = ?", (slug,))
        # Record slug in history for unique allocation tracking
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


def list_panes_slim(session_root: str) -> list[dict]:
    """List all panes without full prompt text (for hot-path display).

    Returns the first 200 characters of each prompt instead of the full blob.
    """
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes").fetchall()
    result = [_row_to_dict(row) for row in rows]
    for r in result:
        if r.get("prompt") and len(r["prompt"]) > 200:
            r["prompt"] = r["prompt"][:200]
    return result


def get_pane_prompt(session_root: str, slug: str) -> str:
    """Get just the prompt text for a single pane."""
    conn = _get_db(session_root)
    row = conn.execute("SELECT prompt FROM panes WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else ""


def update_pane_state(session_root: str, slug: str, new_state: str, force: bool = False) -> None:
    """Update the state field of a pane record.

    Enforces VALID_TRANSITIONS unless *force* is True.
    Same-state transitions are no-ops.
    Uses an atomic UPDATE … WHERE to avoid read-check-write races.
    Raises IllegalTransitionError for disallowed transitions.
    """
    _validate_state(new_state)

    def _do() -> bool:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row

        if force:
            # Skip transition validation — unconditional update.
            cur = conn.execute(
                "UPDATE panes SET state = ? WHERE slug = ? AND state != ?",
                (new_state, slug, new_state),
            )
        else:
            # Build the set of states that are allowed to transition to new_state.
            allowed_from = [
                st for st, targets in VALID_TRANSITIONS.items() if new_state in targets
            ]
            if not allowed_from:
                # No state can legally reach new_state.  Same-state is still a no-op.
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
                # Either slug missing, already at new_state, or illegal transition.
                row = conn.execute("SELECT state FROM panes WHERE slug = ?", (slug,)).fetchone()
                if row is not None and row["state"] != new_state:
                    raise IllegalTransitionError(row["state"], new_state, slug)
                # slug missing or already at new_state — no-op.
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
    """Set a completion state without raising on late terminal races.

    This helper is intentionally narrow: it only applies to completion-path
    states that may race across wait, done detection, manual signals, and
    timeout handling. Normal transition enforcement remains in
    ``update_pane_state()``.

    Returns the persisted state and whether this call changed it.
    """
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
    """Update metadata fields on a specific pane.

    Known keys are written to typed columns. Unknown keys fall back to
    the legacy metadata JSON blob (logged as a warning).
    """
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


def get_preserved_artifacts(pane_record: dict | None) -> dict | None:
    """Return preserved-artifact metadata when present."""
    if not pane_record:
        return None
    reason = pane_record.get("preserve_reason", "")
    if not reason:
        return None
    return {
        "reason": reason,
        "recoverable": bool(pane_record.get("preserve_recoverable", 0)),
    }


def mark_preserved_artifacts(
    session_root: str,
    slug: str,
    *,
    reason: str,
    recoverable: bool,
    state: str | None = None,
    failure_stage: str | None = None,
    preserved_paths: list[str] | tuple[str, ...] = (),
) -> None:
    """Persist preserved-artifact metadata for a pane kept for inspection."""
    pane = get_pane(session_root, slug)
    if pane is None:
        return

    paths: list[str] = []
    for candidate in (
        *preserved_paths,
        str(pane.get("worktree_path", "")),
        str(Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"),
    ):
        if candidate and candidate not in paths:
            paths.append(candidate)

    set_pane_metadata(
        session_root,
        slug,
        preserve_reason=reason,
        preserve_recoverable=int(recoverable),
    )


def clear_preserved_artifacts(session_root: str, slug: str) -> None:
    """Remove preserved-artifact metadata once a pane is resumed or cleaned."""
    set_pane_metadata(session_root, slug, preserve_reason=None, preserve_recoverable=0)


def record_failure(session_root: str, slug: str, failure_hash: str) -> int:
    """Record a failure hash for circuit-breaker detection.

    Tracks failure hashes in pane metadata under ``failure_hashes``
    (a JSON object mapping hash -> count).  Returns the count for
    *failure_hash* after incrementing.
    """

    def _do() -> int:
        conn = _get_db(session_root)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT metadata FROM panes WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return 0
        meta: dict = {}
        raw = row["metadata"]
        if raw:
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        hashes = meta.get("failure_hashes", {})
        if not isinstance(hashes, dict):
            hashes = {}
        hashes[failure_hash] = hashes.get(failure_hash, 0) + 1
        count: int = hashes[failure_hash]
        meta["failure_hashes"] = hashes
        conn.execute(
            "UPDATE panes SET metadata = ? WHERE slug = ?",
            (json.dumps(meta), slug),
        )
        conn.commit()
        return count

    return _retry_on_lock(_do)


def get_child_panes(session_root: str, parent_slug: str) -> list[dict]:
    """Return all panes whose parent_slug matches *parent_slug*."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM panes WHERE parent_slug = ?", (parent_slug,)).fetchall()
    return [_row_to_dict(row) for row in rows]


def replace_all_panes(session_root: str, panes: list[dict] | dict) -> None:
    """Replace all panes in the database with the given list.

    Intended for test setup where you need to establish a known state.
    Each dict should have at least a ``slug`` key.
    Accepts either a list of dicts or a dict with a ``panes`` key.
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
