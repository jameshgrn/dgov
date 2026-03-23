"""DAG file parser and execution engine for dgov."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from dgov.dag_graph import compute_tiers, render_dry_run, topological_order
from dgov.dag_parser import DagDefinition, DagRunOptions, DagRunSummary, parse_dag_file

logger = logging.getLogger(__name__)


def _dag_file_hash(path: str) -> str:
    """SHA-256 of the raw DAG file bytes (before parsing)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_dag(
    dag_file: str,
    *,
    dry_run: bool = False,
    tier_limit: int | None = None,
    skip: set[str] | None = None,
    auto_merge: bool = True,
    max_concurrent: int = 0,
) -> DagRunSummary:
    """Execute a DAG file through the DagKernel state machine."""
    dag = parse_dag_file(dag_file)
    if dry_run:
        tiers = compute_tiers(dag.tasks)
        print(render_dry_run(tiers, dag.tasks))
        return DagRunSummary(run_id=0, dag_file=dag_file, status="dry_run")

    # Convert tier_limit to skip set: skip all tasks in tiers > tier_limit
    effective_skip = set(skip or ())
    if tier_limit is not None:
        tiers = compute_tiers(dag.tasks)
        for tier_idx, tier_tasks in enumerate(tiers):
            if tier_idx > tier_limit:
                effective_skip.update(tier_tasks)

    return run_dag_via_kernel(
        dag,
        dag_key=str(Path(dag_file).resolve()),
        definition_hash=_dag_file_hash(dag_file),
        skip=effective_skip or None,
        auto_merge=auto_merge,
        max_concurrent=max_concurrent,
    )


def run_dag_via_kernel(
    dag: DagDefinition,
    *,
    dag_key: str,
    definition_hash: str,
    skip: set[str] | None = None,
    auto_merge: bool = True,
    max_concurrent: int = 0,
) -> DagRunSummary:
    """Execute a DAG through the DagKernel state machine."""
    from datetime import datetime, timezone

    from dgov.executor import run_dag_kernel
    from dgov.persistence import create_dag_run, emit_event, update_dag_run

    session_root = dag.session_root
    options = DagRunOptions(skip=frozenset(skip or ()), auto_merge=auto_merge)

    # Create the DB run record
    tiers = compute_tiers(dag.tasks)
    state_json = {
        "definition_hash": definition_hash,
        "tiers": tiers,
        "topological_order": topological_order(dag.tasks),
        "options": {
            "skip": sorted(options.skip),
            "auto_merge": options.auto_merge,
        },
    }
    run_id = create_dag_run(
        session_root,
        dag_key,
        datetime.now(timezone.utc).isoformat(),
        "running",
        0,
        state_json,
    )
    emit_event(session_root, "dag_started", f"dag/{run_id}", dag_run_id=run_id)

    effective_concurrent = max_concurrent if max_concurrent > 0 else dag.max_concurrent

    result = run_dag_kernel(
        dag.project_root,
        dag,
        session_root=session_root,
        run_id=run_id,
        auto_merge=auto_merge,
        max_concurrent=effective_concurrent,
        skip=frozenset(skip or ()),
        progress=lambda msg: logger.info("DAG[%d] %s", run_id, msg),
    )

    # Cleanup orphaned panes from failed/partial runs
    from dgov.executor import run_close_only
    from dgov.persistence import list_dag_tasks

    task_rows = list_dag_tasks(session_root, run_id)
    for row in task_rows:
        pane_slug = row.get("pane_slug", "")
        status = row.get("status", "")
        if pane_slug and status not in ("merged", "closed"):
            try:
                run_close_only(dag.project_root, pane_slug, session_root=session_root, force=True)
            except Exception:
                logger.debug("Cleanup failed for %s", pane_slug, exc_info=True)

    # Finalize DB
    update_dag_run(session_root, run_id, status=result.status)
    emit_event(
        session_root,
        "dag_completed" if result.status == "completed" else "dag_failed",
        f"dag/{run_id}",
        dag_run_id=run_id,
        status=result.status,
    )

    return DagRunSummary(
        run_id=run_id,
        dag_file=dag_key,
        status=result.status,
        succeeded=result.merged,
        merged=result.merged,
        failed=result.failed,
        skipped=result.skipped,
        blocked=result.blocked,
    )


def merge_dag(dag_file: str) -> DagRunSummary:
    """Merge an awaiting_merge DAG run in canonical topological order."""
    from dgov.persistence import (
        emit_event,
        ensure_dag_tables,
        get_open_dag_run,
        list_dag_tasks,
        update_dag_run,
        upsert_dag_task,
    )

    dag = parse_dag_file(dag_file)
    abs_path = str(Path(dag_file).resolve())
    session_root = os.path.abspath(dag.session_root)
    ensure_dag_tables(session_root)

    existing = get_open_dag_run(session_root, abs_path)
    if not existing or existing["status"] != "awaiting_merge":
        raise ValueError(f"No awaiting_merge run found for {abs_path}")

    run_id = existing["id"]
    task_rows = list_dag_tasks(session_root, run_id)
    task_states = {r["slug"]: r["status"] for r in task_rows}
    pane_slugs = {r["slug"]: r["pane_slug"] for r in task_rows if r["pane_slug"]}

    ready = [s for s, st in task_states.items() if st == "reviewed_pass"]
    if not ready:
        raise ValueError("No reviewed_pass tasks to merge")

    # Merge in topological order using executor
    from dgov.executor import run_merge_only

    topo = topological_order(dag.tasks)
    ordered = [s for s in topo if s in ready]
    merged: list[str] = []

    for task_slug in ordered:
        pane_slug = pane_slugs.get(task_slug, "")
        if not pane_slug:
            continue
        result = run_merge_only(
            dag.project_root,
            pane_slug,
            session_root=session_root,
            resolve=dag.merge_resolve,
            squash=dag.merge_squash,
            message=dag.tasks[task_slug].commit_message or None,
        )
        if result.error:
            update_dag_run(session_root, run_id, status="failed")
            emit_event(
                session_root,
                "dag_failed",
                f"dag/{run_id}",
                dag_run_id=run_id,
                error="merge_conflict",
            )
            return DagRunSummary(
                run_id=run_id,
                dag_file=abs_path,
                status="failed",
                merged=merged,
                failed=[task_slug],
            )
        merged.append(task_slug)
        task_states[task_slug] = "merged"
        upsert_dag_task(session_root, run_id, task_slug, "merged", dag.tasks[task_slug].agent)
        emit_event(session_root, "dag_task_completed", task_slug, dag_run_id=run_id)

    update_dag_run(session_root, run_id, status="completed")
    emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)

    succeeded = [s for s, st in task_states.items() if st in ("merged", "reviewed_pass")]
    failed = [s for s, st in task_states.items() if st in ("failed", "reviewed_fail")]
    return DagRunSummary(
        run_id=run_id,
        dag_file=abs_path,
        status="completed",
        succeeded=succeeded,
        merged=merged,
        failed=failed,
    )
