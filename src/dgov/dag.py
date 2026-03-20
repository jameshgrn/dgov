"""DAG file parser and execution engine for dgov."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

from dgov import executor as _executor
from dgov.dag_graph import (  # noqa: F401 — re-exported for batch/cli/tests
    compute_tiers,
    render_dry_run,
    topological_order,
    transitive_dependents,
    validate_dag,
)
from dgov.dag_parser import (  # noqa: F401 — re-exported for batch/cli/tests
    DagDefinition,
    DagFileSpec,
    DagRunOptions,
    DagRunSummary,
    DagTaskSpec,
    parse_dag_file,
)

logger = logging.getLogger(__name__)


def run_dispatch_preflight(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
    """Resolve dispatch preflight dynamically to preserve test patch points."""
    return _executor.run_dispatch_preflight(*args, **kwargs)


def run_merge_only(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
    """Resolve merge execution dynamically to preserve test patch points."""
    return _executor.run_merge_only(*args, **kwargs)


def run_post_dispatch_lifecycle(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
    """Resolve lifecycle execution dynamically to preserve test patch points."""
    return _executor.run_post_dispatch_lifecycle(*args, **kwargs)


_DAG_PROGRESS_EVENTS = (
    "dag_task_dispatched",
    "dag_task_completed",
    "dag_task_failed",
    "dag_task_escalated",
    "dag_completed",
    "dag_failed",
    "pane_done",
    "pane_timed_out",
    "pane_merged",
    "pane_merge_failed",
    "pane_auto_retried",
    "pane_retry_spawned",
    "pane_escalated",
    "pane_superseded",
    "pane_closed",
)


def _progress(msg: str) -> None:
    """Print a progress message to stderr for DAG execution visibility."""
    import sys

    print(f"[dag] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Single-tier execution helpers
# ---------------------------------------------------------------------------


def _merge_tasks_in_order(
    dag: DagDefinition,
    ready: list[str],
    pane_slugs: dict[str, str],
    session_root: str,
    run_id: int,
) -> tuple[list[str], dict | None]:
    """Merge reviewed-pass tasks in canonical order.

    Returns (merged_list, error_or_None).
    """
    from dgov.persistence import emit_event, upsert_dag_task

    topo = topological_order(dag.tasks)
    ordered = [s for s in topo if s in ready]
    merged: list[str] = []

    for task_slug in ordered:
        pane_slug = pane_slugs[task_slug]
        logger.info("Merging %s (pane %s)", task_slug, pane_slug)
        commit_message = dag.tasks[task_slug].commit_message
        if commit_message:
            result = run_merge_only(
                dag.project_root,
                pane_slug,
                session_root=session_root,
                resolve=dag.merge_resolve,
                squash=dag.merge_squash,
                message=commit_message,
            )
        else:
            result = run_merge_only(
                dag.project_root,
                pane_slug,
                session_root=session_root,
                resolve=dag.merge_resolve,
                squash=dag.merge_squash,
            )
        if result.error:
            logger.error("Merge error for %s: %s", task_slug, result.error)
            return merged, result.merge_result or {"error": result.error}

        merged.append(task_slug)
        _progress(f"  merged {task_slug}")

        # Run post-merge check if defined
        check_cmd = dag.tasks[task_slug].post_merge_check
        if check_cmd:
            check_env = os.environ.copy()
            check_env["DGOV_TASK_SLUG"] = task_slug
            check_env["DGOV_MERGE_SHA"] = subprocess.run(
                ["git", "-C", dag.project_root, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            check_env["DGOV_CHANGED_FILES"] = "\n".join(
                f
                for f in subprocess.run(
                    ["git", "-C", dag.project_root, "diff", "--name-only", "HEAD~1", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .splitlines()
                if f
            )
            check_result = subprocess.run(
                check_cmd,
                shell=True,
                cwd=dag.project_root,
                capture_output=True,
                text=True,
                env=check_env,
            )
            if check_result.returncode != 0:
                rollback = subprocess.run(
                    ["git", "-C", dag.project_root, "reset", "--keep", "HEAD~1"],
                    capture_output=True,
                    text=True,
                )
                logger.error(
                    "Post-merge check failed for %s: %s",
                    task_slug,
                    check_result.stderr or check_result.stdout,
                )
                if rollback.returncode == 0:
                    _progress(f"  post_merge_check FAILED for {task_slug}, rolled back")
                    return merged[:-1], {
                        "error": f"Post-merge check failed for {task_slug}",
                        "check_command": check_cmd,
                        "check_stderr": check_result.stderr.strip(),
                        "check_stdout": check_result.stdout.strip(),
                        "rollback_performed": True,
                    }

                upsert_dag_task(
                    session_root, run_id, task_slug, "merged", dag.tasks[task_slug].agent
                )
                emit_event(session_root, "dag_task_completed", task_slug, dag_run_id=run_id)
                _progress(
                    f"  post_merge_check FAILED for {task_slug}, rollback skipped;"
                    " merge commit preserved"
                )
                return merged, {
                    "error": f"Post-merge check failed for {task_slug}",
                    "check_command": check_cmd,
                    "check_stderr": check_result.stderr.strip(),
                    "check_stdout": check_result.stdout.strip(),
                    "rollback_performed": False,
                    "rollback_error": rollback.stderr.strip() or rollback.stdout.strip(),
                }

        upsert_dag_task(session_root, run_id, task_slug, "merged", dag.tasks[task_slug].agent)
        emit_event(session_root, "dag_task_completed", task_slug, dag_run_id=run_id)

    return merged, None


def _merge_ready_tasks(
    dag: DagDefinition,
    dag_file: str,
    run_id: int,
    task_states: dict[str, str],
    ready: list[str],
    pane_slugs: dict[str, str],
    session_root: str,
    merged_so_far: list[str] | None = None,
) -> tuple[list[str], DagRunSummary | None]:
    """Merge reviewed-pass tasks and finalize DAG state on conflict."""
    from dgov.persistence import emit_event, update_dag_run

    merged, merge_error = _merge_tasks_in_order(dag, ready, pane_slugs, session_root, run_id)
    for slug in merged:
        task_states[slug] = "merged"

    if merge_error:
        merged_total = [*(merged_so_far or []), *merged]
        update_dag_run(session_root, run_id, status="failed")
        emit_event(
            session_root,
            "dag_failed",
            f"dag/{run_id}",
            dag_run_id=run_id,
            error="merge_conflict",
        )
        return merged, _build_summary(run_id, dag_file, "failed", task_states, merged_total, dag)

    return merged, None


# ---------------------------------------------------------------------------
# Multi-tier orchestration
# ---------------------------------------------------------------------------


def _dag_file_hash(path: str) -> str:
    """SHA-256 of the raw DAG file bytes (before parsing)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_dag(
    dag_file: str,
    *,
    dry_run: bool = False,
    tier_limit: int | None = None,
    skip: set[str] | None = None,
    max_retries: int = 1,
    auto_merge: bool = True,
    max_concurrent: int = 0,
) -> DagRunSummary:
    """Execute a DAG file through the DagKernel state machine."""
    dag = parse_dag_file(dag_file)
    if dry_run:
        tiers = compute_tiers(dag.tasks)
        print(render_dry_run(tiers, dag.tasks))
        return DagRunSummary(run_id=0, dag_file=dag_file, status="dry_run")

    return run_dag_via_kernel(
        dag,
        dag_key=str(Path(dag_file).resolve()),
        definition_hash=_dag_file_hash(dag_file),
        skip=skip,
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
        progress=lambda msg: logger.info("DAG[%d] %s", run_id, msg),
    )

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
    )


def _build_summary(
    run_id: int,
    dag_file: str,
    status: str,
    task_states: dict[str, str],
    merged: list[str],
    dag: DagDefinition,
) -> DagRunSummary:
    succeeded = [s for s, st in task_states.items() if st in ("merged", "reviewed_pass")]
    failed = [s for s, st in task_states.items() if st in ("failed", "reviewed_fail")]
    skipped = [s for s, st in task_states.items() if st == "skipped"]
    unmerged = [s for s, st in task_states.items() if st == "reviewed_pass"]
    return DagRunSummary(
        run_id=run_id,
        dag_file=dag_file,
        status=status,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        merged=merged,
        unmerged=unmerged,
    )


def merge_dag(dag_file: str) -> DagRunSummary:
    """Merge an awaiting_merge DAG run in canonical topological order."""
    from dgov.persistence import (
        emit_event,
        ensure_dag_tables,
        get_open_dag_run,
        list_dag_tasks,
        update_dag_run,
    )

    dag = parse_dag_file(dag_file)
    abs_path = str(Path(dag_file).resolve())
    session_root = os.path.abspath(dag.session_root)
    ensure_dag_tables(session_root)

    existing = get_open_dag_run(session_root, abs_path)
    if not existing or existing["status"] != "awaiting_merge":
        raise ValueError(f"No awaiting_merge run found for {abs_path}")

    run_id = existing["id"]
    file_hash = _dag_file_hash(dag_file)
    stored_hash = existing.get("state_json", {}).get("dag_sha256", "")
    if stored_hash and stored_hash != file_hash:
        raise ValueError("DAG file has changed since the run was created")

    task_rows = list_dag_tasks(session_root, run_id)
    task_states = {r["slug"]: r["status"] for r in task_rows}
    pane_slugs = {r["slug"]: r["pane_slug"] for r in task_rows if r["pane_slug"]}

    ready = [s for s, st in task_states.items() if st == "reviewed_pass"]
    if not ready:
        raise ValueError("No reviewed_pass tasks to merge")

    merged, failed_summary = _merge_ready_tasks(
        dag,
        dag_file,
        run_id,
        task_states,
        ready,
        pane_slugs,
        session_root,
    )
    if failed_summary is not None:
        return failed_summary

    update_dag_run(session_root, run_id, status="completed")
    emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)
    return _build_summary(run_id, dag_file, "completed", task_states, merged, dag)
