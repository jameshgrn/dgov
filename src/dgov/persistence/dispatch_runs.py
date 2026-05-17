"""Persistence operations for DispatchRun records."""

from __future__ import annotations

import json
import logging
import sqlite3

from dgov.dispatch_run import DispatchRun
from dgov.persistence.connection import _get_db, _retry_on_lock
from dgov.run_source import normalize_run_source

logger = logging.getLogger(__name__)

_DISPATCH_RUN_COLUMNS = (
    "id",
    "from_plan_id",
    "unit_slug",
    "worktree_id",
    "branch",
    "base_commit",
    "agent_model",
    "effective_sop_set_hash",
    "drift_against_plan",
    "drift_evidence",
    "run_source",
    "retried_from",
    "forked_from",
    "retry_index",
    "fork_depth",
    "dispatched_by",
    "dispatched_at",
    "state",
    "exit_code",
    "last_error",
    "output_dir",
    "prompt_tokens",
    "completion_tokens",
    "iteration_count",
    "terminated_at",
)


def _decode_drift_evidence(raw_evidence: object, run_id: object) -> tuple[str, ...]:
    if not isinstance(raw_evidence, str):
        return ()
    try:
        decoded = json.loads(raw_evidence or "[]")
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Corrupt drift_evidence for dispatch run %s: %s", run_id, exc)
        return ()
    if not isinstance(decoded, list):
        logger.warning("Invalid drift_evidence for dispatch run %s: expected JSON array", run_id)
        return ()
    return tuple(str(item) for item in decoded)


def _serialize_dispatch_run(dispatch_run: DispatchRun) -> dict[str, object]:
    return {
        "id": dispatch_run.id,
        "from_plan_id": dispatch_run.from_plan_id,
        "unit_slug": dispatch_run.unit_slug,
        "worktree_id": dispatch_run.worktree_id,
        "branch": dispatch_run.branch,
        "base_commit": dispatch_run.base_commit,
        "agent_model": dispatch_run.agent_model,
        "effective_sop_set_hash": dispatch_run.effective_sop_set_hash,
        "drift_against_plan": int(dispatch_run.drift_against_plan),
        "drift_evidence": json.dumps(list(dispatch_run.drift_evidence)),
        "run_source": dispatch_run.run_source,
        "retried_from": dispatch_run.retried_from,
        "forked_from": dispatch_run.forked_from,
        "retry_index": dispatch_run.retry_index,
        "fork_depth": dispatch_run.fork_depth,
        "dispatched_by": dispatch_run.dispatched_by,
        "dispatched_at": dispatch_run.dispatched_at.isoformat(),
        "state": dispatch_run.state,
        "exit_code": dispatch_run.exit_code,
        "last_error": dispatch_run.last_error,
        "output_dir": dispatch_run.output_dir,
        "prompt_tokens": dispatch_run.prompt_tokens,
        "completion_tokens": dispatch_run.completion_tokens,
        "iteration_count": dispatch_run.iteration_count,
        "terminated_at": (
            dispatch_run.terminated_at.isoformat()
            if dispatch_run.terminated_at is not None
            else None
        ),
    }


def _row_to_dispatch_run_dict(row: sqlite3.Row) -> dict:
    row_dict = dict(row)
    row_dict["drift_against_plan"] = bool(row_dict["drift_against_plan"])
    row_dict["drift_evidence"] = _decode_drift_evidence(
        row_dict["drift_evidence"],
        row_dict.get("id", "?"),
    )
    return row_dict


def _insert_dispatch_run_dict(conn: sqlite3.Connection, row_dict: dict[str, object]) -> None:
    columns = ", ".join(_DISPATCH_RUN_COLUMNS)
    placeholders = ", ".join("?" for _ in _DISPATCH_RUN_COLUMNS)
    values = [row_dict[column] for column in _DISPATCH_RUN_COLUMNS]
    conn.execute(
        f"INSERT OR REPLACE INTO dispatch_runs ({columns}) VALUES ({placeholders})",
        values,
    )


def save_dispatch_run(session_root: str, dispatch_run: DispatchRun) -> None:
    """Insert or replace a DispatchRun row. Idempotent upsert."""

    def _do() -> None:
        conn = _get_db(session_root)
        _insert_dispatch_run_dict(conn, _serialize_dispatch_run(dispatch_run))
        conn.commit()

    _retry_on_lock(_do)


def get_dispatch_run(session_root: str, dispatch_run_id: str) -> dict | None:
    """Return a DispatchRun row dict by id, or None when absent."""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = ?", (dispatch_run_id,)).fetchone()
    return _row_to_dispatch_run_dict(row) if row else None


def list_dispatch_runs(
    session_root: str,
    *,
    plan_id: str | None = None,
    unit_slug: str | None = None,
    state: str | None = None,
    run_source: str | None = None,
) -> list[dict]:
    """List DispatchRun rows filtered by optional keys."""
    clauses: list[str] = []
    values: list[str] = []
    if plan_id is not None:
        clauses.append("from_plan_id = ?")
        values.append(plan_id)
    if unit_slug is not None:
        clauses.append("unit_slug = ?")
        values.append(unit_slug)
    if state is not None:
        clauses.append("state = ?")
        values.append(state)
    if run_source is not None:
        clauses.append("run_source = ?")
        values.append(normalize_run_source(run_source))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM dispatch_runs{where} ORDER BY dispatched_at ASC",
        values,
    ).fetchall()
    return [_row_to_dispatch_run_dict(row) for row in rows]


def get_dispatch_runs_for_unit(session_root: str, unit_slug: str) -> list[dict]:
    """Return all DispatchRuns for a unit in dispatch order."""
    return list_dispatch_runs(session_root, unit_slug=unit_slug)


__all__ = [
    "get_dispatch_run",
    "get_dispatch_runs_for_unit",
    "list_dispatch_runs",
    "save_dispatch_run",
]
