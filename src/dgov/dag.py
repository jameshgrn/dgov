"""DAG file parser and execution engine for dgov."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from dgov.dag_graph import compute_tiers, render_dry_run, topological_order
from dgov.dag_parser import DagDefinition, DagRunSummary, parse_dag_file
from dgov.kernel import DagKernel, DagTaskState

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
    plan_evals: list[dict] | None = None,
    unit_eval_links: list[dict] | None = None,
) -> DagRunSummary:
    """Submit a DAG for headless execution by the monitor daemon."""
    from dataclasses import asdict
    from datetime import datetime, timezone

    from dgov.persistence import create_dag_run, emit_event, replace_dag_plan_contract

    session_root = dag.session_root

    # Initialize the kernel to get the starting state_json
    deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
    review_agents = {slug: t.review_agent for slug, t in dag.tasks.items() if t.review_agent}

    kernel = DagKernel(
        deps=deps,
        auto_merge=auto_merge,
        max_concurrent=max_concurrent or dag.max_concurrent,
        skip=frozenset(skip or ()),
        review_agents=review_agents,
        max_retries=dag.default_max_retries,
    )

    # Serialize definition for headless reconstruction
    def_json = {
        "name": dag.name,
        "default_max_retries": dag.default_max_retries,
        "merge_resolve": dag.merge_resolve,
        "merge_squash": dag.merge_squash,
        "max_concurrent": dag.max_concurrent,
        "tasks": {slug: asdict(t) for slug, t in dag.tasks.items()},
    }

    # Create the DB run record
    run_id = create_dag_run(
        session_root,
        dag_key,
        datetime.now(timezone.utc).isoformat(),
        "running",
        0,
        kernel.to_dict(),
        definition_json=def_json,
    )
    if plan_evals or unit_eval_links:
        replace_dag_plan_contract(
            session_root,
            run_id,
            evals=plan_evals or [],
            unit_eval_links=unit_eval_links or [],
        )

    # Ensure the headless engine is running
    from dgov.monitor import ensure_monitor_running

    ensure_monitor_running(dag.project_root, session_root=session_root)

    # Notify monitor
    emit_event(session_root, "dag_started", f"dag/{run_id}", dag_run_id=run_id)

    return DagRunSummary(
        run_id=run_id,
        dag_file=dag_key,
        status="submitted",
        definition_hash=definition_hash,
        succeeded=[],
        merged=[],
        failed=[],
        skipped=[],
        blocked=[],
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

    ready = [s for s, st in task_states.items() if st == DagTaskState.MERGE_READY]
    if not ready:
        raise ValueError("No merge_ready tasks to merge")

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
                error="merge_failed",
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

    succeeded = [
        s for s, st in task_states.items() if st in (DagTaskState.MERGED, DagTaskState.MERGE_READY)
    ]
    failed = [
        s
        for s, st in task_states.items()
        if st in (DagTaskState.FAILED, DagTaskState.REVIEWED_FAIL)
    ]
    return DagRunSummary(
        run_id=run_id,
        dag_file=abs_path,
        status="completed",
        succeeded=succeeded,
        merged=merged,
        failed=failed,
    )
