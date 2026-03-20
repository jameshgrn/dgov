"""DAG file parser and execution engine for dgov."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

from dgov import executor as _executor
from dgov.context_packet import build_context_packet
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
from dgov.executor import PostDispatchResult

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


def _wait_for_dag_progress_event(
    session_root: str,
    run_id: int,
    task_slugs: tuple[str, ...],
    *,
    after_id: int,
    timeout_s: float = 1.0,
) -> list[dict]:
    """Wait for a new event that could advance DAG scheduling state."""
    from dgov.persistence import wait_for_events

    panes = (f"dag/{run_id}", *task_slugs)
    return wait_for_events(
        session_root,
        after_id=after_id,
        panes=panes,
        event_types=_DAG_PROGRESS_EVENTS,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Single-tier execution helpers
# ---------------------------------------------------------------------------


def _dispatch_task(
    dag: DagDefinition,
    task: DagTaskSpec,
    run_id: int,
    session_root: str,
) -> dict:
    """Dispatch a single task as a worker pane. Returns pane info dict."""
    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event, upsert_dag_task

    touches = [*task.files.create, *task.files.edit, *task.files.delete]
    packet = build_context_packet(
        task.prompt,
        file_claims=touches,
        commit_message=task.commit_message,
    )
    report = run_dispatch_preflight(
        dag.project_root,
        task.agent,
        session_root=session_root,
        packet=packet,
    )
    if not report.passed:
        failed_checks = [c.message for c in report.checks if not c.passed and c.critical]
        raise RuntimeError(f"Preflight failed: {'; '.join(failed_checks)}")

    logger.info("Dispatching task %s with agent %s", task.slug, task.agent)
    pane = create_worker_pane(
        project_root=dag.project_root,
        prompt=task.prompt,
        agent=task.agent,
        permission_mode=task.permission_mode,
        slug=task.slug,
        session_root=session_root,
        context_packet=packet,
    )
    pane_slug = pane.slug
    upsert_dag_task(
        session_root,
        run_id,
        task.slug,
        "dispatched",
        task.agent,
        attempt=1,
        pane_slug=pane_slug,
    )
    emit_event(
        session_root,
        "dag_task_dispatched",
        task.slug,
        dag_run_id=run_id,
        agent=task.agent,
        attempt=1,
    )
    return {"task_slug": task.slug, "pane_slug": pane_slug, "agent": task.agent}


def _wait_for_tier(
    dag: DagDefinition,
    active_panes: dict[str, dict],
    session_root: str,
) -> dict[str, dict]:
    """Run the canonical post-dispatch lifecycle for each pane in a tier."""
    results: dict[str, dict] = {}
    for task_slug, pane_info in active_panes.items():
        pane_slug = pane_info["pane_slug"]
        task = dag.tasks[task_slug]
        result = run_post_dispatch_lifecycle(
            dag.project_root,
            pane_slug,
            session_root=session_root,
            timeout=task.timeout_s,
            max_retries=0,
            auto_merge=False,
        )
        results[task_slug] = {"result": result, "pane_slug": result.slug}

    return results


def _is_task_terminal_success(
    task_slug: str,
    task_states: dict[str, str],
    auto_merge: bool,
) -> bool:
    """Check if a task has reached terminal-success state for its mode.

    - If auto_merge=True: terminal success means "merged"
    - If auto_merge=False: terminal success means "reviewed_pass" or "merged"
    """
    state = task_states.get(task_slug)
    if auto_merge:
        return state == "merged"
    return state in ("reviewed_pass", "merged")


def _are_all_dependencies_met(
    task: DagTaskSpec,
    task_states: dict[str, str],
    auto_merge: bool,
) -> bool:
    """Check if all dependencies are satisfied for this task to run.

    A dependency is satisfied when it has reached terminal-success state:
    - merged (if auto_merge=True)
    - reviewed_pass or merged (if auto_merge=False)

    Returns True if no dependencies (leaf task).
    """
    if not task.depends_on:
        return True
    return all(_is_task_terminal_success(dep, task_states, auto_merge) for dep in task.depends_on)


def _mark_transitive_skipped(
    dag: DagDefinition,
    failed_or_skipped: set[str],
    task_states: dict[str, str],
    run_id: int,
    session_root: str,
) -> None:
    """Mark all transitive dependents of failed/skipped tasks as skipped."""
    from dgov.persistence import upsert_dag_task

    newly_skipped = transitive_dependents(dag.tasks, failed_or_skipped)
    for slug in newly_skipped:
        if task_states.get(slug) not in ("merged", "reviewed_pass", "failed", "skipped"):
            task_states[slug] = "skipped"
            upsert_dag_task(session_root, run_id, slug, "skipped", dag.tasks[slug].agent)


def _get_ready_tasks(
    dag: DagDefinition,
    task_states: dict[str, str],
    tier_limit: int | None,
    auto_merge: bool,
) -> list[str]:
    """Get all tasks that are ready to dispatch based on dependency readiness.

    A task is ready when:
    1. It hasn't been dispatched/reviewed/failed/skipped yet
    2. All its dependencies are in terminal-success state
    3. Its computed tier index <= tier_limit (if tier_limit is set)
    """
    tiers = compute_tiers(dag.tasks)
    task_to_tier: dict[str, int] = {}
    for tier_idx, tier_tasks in enumerate(tiers):
        for slug in tier_tasks:
            task_to_tier[slug] = tier_idx

    ready: list[str] = []
    for slug, task in dag.tasks.items():
        # Skip if already processed
        current_state = task_states.get(slug)
        if current_state not in ("", None):
            continue

        # Check tier limit
        if tier_limit is not None and task_to_tier.get(slug, 0) > tier_limit:
            continue

        # Check dependencies are met
        if _are_all_dependencies_met(task, task_states, auto_merge):
            ready.append(slug)

    return sorted(ready)


def _wait_for_any_completion(
    dag: DagDefinition,
    active_pane_slugs: dict[str, str],
    session_root: str,
    task_states: dict[str, str],
    run_id: int,
) -> dict[str, str]:
    """Wait for any active pane to complete and process it.

    Returns dict mapping task_slug to new state.
    This enables event-driven progression instead of blocking on entire tiers.
    """
    results: dict[str, str] = {}

    # Wait for each pane individually (sequential wait for simplicity)
    for task_slug, pane_slug in active_pane_slugs.items():
        task = dag.tasks[task_slug]
        lifecycle = run_post_dispatch_lifecycle(
            dag.project_root,
            pane_slug,
            session_root=session_root,
            timeout=task.timeout_s,
            max_retries=0,
            auto_merge=False,
        )
        results[task_slug] = _persist_task_lifecycle_result(
            dag,
            task_slug,
            lifecycle,
            session_root,
            run_id,
            task_states,
        )

    return results


def _dag_task_status_for_lifecycle(lifecycle: PostDispatchResult) -> str:
    """Map canonical executor output to the DAG task status vocabulary."""
    if lifecycle.state == "completed":
        return "merged"
    if lifecycle.state == "reviewed_pass":
        return "reviewed_pass"
    if lifecycle.state == "review_pending":
        return "reviewed_fail"
    return "failed"


def _persist_task_lifecycle_result(
    dag: DagDefinition,
    task_slug: str,
    lifecycle: PostDispatchResult,
    session_root: str,
    run_id: int,
    task_states: dict[str, str],
) -> str:
    """Persist the canonical executor lifecycle outcome for a DAG task."""
    from dgov.persistence import update_pane_state, upsert_dag_task

    task = dag.tasks[task_slug]
    pane_slug = lifecycle.slug
    status = _dag_task_status_for_lifecycle(lifecycle)
    error = getattr(lifecycle, "error", None)

    task_states[task_slug] = status
    if status == "merged":
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "merged",
            task.agent,
            pane_slug=pane_slug,
        )
        return status

    if status == "reviewed_pass":
        update_pane_state(session_root, pane_slug, "reviewed_pass", force=True)
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "reviewed_pass",
            task.agent,
            pane_slug=pane_slug,
        )
        _progress(f"  reviewed {task_slug}: pass")
        return status

    if status == "reviewed_fail":
        update_pane_state(session_root, pane_slug, "reviewed_fail", force=True)
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "reviewed_fail",
            task.agent,
            pane_slug=pane_slug,
        )
        _progress(f"  reviewed {task_slug}: fail")
        return status

    upsert_dag_task(
        session_root,
        run_id,
        task_slug,
        status,
        task.agent,
        pane_slug=pane_slug,
        error=error or "post-dispatch lifecycle failed",
    )
    return status


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


def _reconcile_orphan_panes(
    dag: DagDefinition,
    run_id: int,
    session_root: str,
) -> None:
    """Scan for orphan panes from a previous governor crash and reconcile."""
    from dgov.lifecycle import close_worker_pane
    from dgov.persistence import list_dag_tasks, list_panes_slim, upsert_dag_task

    dag_tasks = list_dag_tasks(session_root, run_id)
    known_pane_slugs = {t["pane_slug"] for t in dag_tasks if t["pane_slug"]}
    task_slugs = set(dag.tasks)

    panes = list_panes_slim(session_root)
    for pane in panes:
        pane_slug = pane.get("slug", "")
        if pane_slug in known_pane_slugs:
            continue
        # Check if this pane matches a DAG task slug pattern
        base_slug = pane_slug.split("-esc-")[0].split("-retry-")[0]
        if base_slug not in task_slugs:
            continue
        # Orphan pane found
        state = pane.get("state", "")
        logger.warning("Found orphan pane %s (state=%s) for task %s", pane_slug, state, base_slug)
        if state in ("done", "running", "waiting"):
            # Adopt it
            upsert_dag_task(
                session_root,
                run_id,
                base_slug,
                "dispatched",
                pane.get("agent", "unknown"),
                pane_slug=pane_slug,
            )
        else:
            # Dead pane, close it
            try:
                close_worker_pane(
                    dag.project_root, pane_slug, session_root=session_root, force=True
                )
            except Exception:
                pass


def _start_or_resume_run(
    dag_file: str,
    options: DagRunOptions,
    session_root: str,
) -> tuple[int, DagDefinition, dict[str, str]]:
    """Start a new DAG run or resume an existing one.

    Returns (run_id, dag_definition, task_states).
    """
    dag = parse_dag_file(dag_file)
    return _start_or_resume_run_definition(
        dag_key=str(Path(dag_file).resolve()),
        dag=dag,
        definition_hash=_dag_file_hash(dag_file),
        options=options,
        session_root=session_root,
    )


def _start_or_resume_run_definition(
    *,
    dag_key: str,
    dag: DagDefinition,
    definition_hash: str,
    options: DagRunOptions,
    session_root: str,
) -> tuple[int, DagDefinition, dict[str, str]]:
    """Start or resume a DAG run for an already-built DAG definition."""
    from datetime import datetime, timezone

    from dgov.persistence import (
        create_dag_run,
        emit_event,
        ensure_dag_tables,
        get_open_dag_run,
        list_dag_tasks,
    )

    session_root = os.path.abspath(session_root)
    ensure_dag_tables(session_root)

    existing = get_open_dag_run(session_root, dag_key)
    if existing:
        stored_hash = existing.get("state_json", {}).get("dag_sha256", "")
        if stored_hash and stored_hash != definition_hash:
            raise ValueError(
                f"DAG file has changed since run {existing['id']} started. "
                f"Stored hash: {stored_hash[:12]}..., current: {definition_hash[:12]}..."
            )
        run_id = existing["id"]
        logger.info("Resuming DAG run %d", run_id)
        _reconcile_orphan_panes(dag, run_id, session_root)
        # Reconstruct task_states from dag_tasks rows (source of truth)
        task_rows = list_dag_tasks(session_root, run_id)
        task_states = {r["slug"]: r["status"] for r in task_rows}
        return run_id, dag, task_states

    # New run
    tiers = compute_tiers(dag.tasks)
    state_json = {
        "dag_sha256": definition_hash,
        "dag_name": dag.name,
        "tiers": tiers,
        "topological_order": topological_order(dag.tasks),
        "options": {
            "tier_limit": options.tier_limit,
            "skip": sorted(options.skip),
            "max_retries": options.max_retries,
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
    logger.info("Started new DAG run %d for %s", run_id, dag.name)
    return run_id, dag, {}


def run_dag_definition(
    dag: DagDefinition,
    *,
    dag_key: str | None = None,
    definition_hash: str,
    dry_run: bool = False,
    tier_limit: int | None = None,
    skip: set[str] | None = None,
    max_retries: int = 1,
    auto_merge: bool = True,
    max_concurrent: int = 0,
) -> DagRunSummary:
    """Execute an already-built DAG definition through the canonical scheduler."""
    from dgov.lifecycle import close_worker_pane
    from dgov.persistence import (
        emit_event,
        get_dag_task,
        latest_event_id,
        list_dag_tasks,
        update_dag_run,
        upsert_dag_task,
    )

    effective_concurrent = max_concurrent if max_concurrent > 0 else dag.max_concurrent
    options = DagRunOptions(
        dry_run=dry_run,
        tier_limit=tier_limit,
        skip=frozenset(skip or ()),
        max_retries=max_retries,
        auto_merge=auto_merge,
        max_concurrent=effective_concurrent,
    )
    dag_key = dag_key or dag.dag_file

    if dry_run:
        return DagRunSummary(run_id=0, dag_file=dag_key, status="dry_run")

    session_root = os.path.abspath(dag.session_root)
    run_id, dag, task_states = _start_or_resume_run_definition(
        dag_key=dag_key,
        dag=dag,
        definition_hash=definition_hash,
        options=options,
        session_root=session_root,
    )

    # Apply skip + transitive
    skipped = set(options.skip)
    if skipped:
        transitive = transitive_dependents(dag.tasks, skipped)
        skipped |= transitive
        for slug in skipped:
            task_states[slug] = "skipped"
            upsert_dag_task(session_root, run_id, slug, "skipped", dag.tasks[slug].agent)
            # Close already-dispatched panes for newly-skipped tasks
            for s in list(task_states.keys()):
                if s in skipped and task_states.get(s) == "dispatched":
                    try:
                        close_worker_pane(
                            dag.project_root, s, session_root=session_root, force=True
                        )
                    except Exception:
                        pass

    all_merged: list[str] = []
    all_failed: list[str] = []
    pending_merge: list[str] = []  # reviewed_pass tasks waiting for merge
    highest_tier_started: int = -1

    # Main readiness-based execution loop
    while True:
        # Get tasks ready to dispatch
        ready = _get_ready_tasks(dag, task_states, tier_limit, auto_merge)

        # Track active panes (dispatched but not yet reviewed)
        active_panes: dict[str, str] = {}
        for slug, state in task_states.items():
            if state == "dispatched":
                task_row = get_dag_task(session_root, run_id, slug)
                if task_row and task_row.get("pane_slug"):
                    active_panes[slug] = task_row["pane_slug"]

        # Dispatch ready tasks up to concurrency limit
        currently_dispatched = len(active_panes)
        if effective_concurrent <= 0:
            slots_available = len(ready)
        else:
            slots_available = max(0, effective_concurrent - currently_dispatched)

        if ready and slots_available > 0:
            batch = ready[:slots_available]
            for slug in batch:
                task = dag.tasks[slug]
                try:
                    pane_info = _dispatch_task(dag, task, run_id, session_root)
                    task_states[slug] = "dispatched"
                    active_panes[slug] = pane_info["pane_slug"]
                    _progress(f"  dispatched {slug} ({task.agent})")

                    # Update highest tier started
                    tiers = compute_tiers(dag.tasks)
                    for tier_idx, tier_tasks in enumerate(tiers):
                        if slug in tier_tasks:
                            highest_tier_started = max(highest_tier_started, tier_idx)
                            break
                except RuntimeError as exc:
                    if "Concurrency limit" in str(exc):
                        logger.info("Deferred %s due to concurrency limit", slug)
                    else:
                        logger.error("Dispatch failed for %s: %s", slug, exc)
                        task_states[slug] = "failed"
                        upsert_dag_task(
                            session_root, run_id, slug, "failed", task.agent, error=str(exc)
                        )
                        all_failed.append(slug)
                        _mark_transitive_skipped(dag, {slug}, task_states, run_id, session_root)
                except Exception as exc:
                    logger.error("Dispatch failed for %s: %s", slug, exc)
                    task_states[slug] = "failed"
                    upsert_dag_task(
                        session_root, run_id, slug, "failed", task.agent, error=str(exc)
                    )
                    all_failed.append(slug)
                    _mark_transitive_skipped(dag, {slug}, task_states, run_id, session_root)

        # Process completed panes (wait for any that are done)
        if active_panes:
            completed = _wait_for_any_completion(
                dag, active_panes, session_root, task_states, run_id
            )

            # Collect failures for transitive skip
            failures = {slug for slug, state in completed.items() if state == "failed"}
            if failures:
                _mark_transitive_skipped(dag, failures, task_states, run_id, session_root)
                all_failed.extend(failures)

            # Collect reviewed_pass for merging
            passed = [slug for slug, state in completed.items() if state == "reviewed_pass"]
            if passed:
                pending_merge.extend(passed)
                _progress(f"  {len(passed)} task(s) ready for merge")

            merged_now = [slug for slug, state in completed.items() if state == "merged"]
            if merged_now:
                all_merged.extend(merged_now)

        # Merge if we have reviewed_pass tasks and auto_merge is enabled
        if auto_merge and pending_merge:
            pane_slugs: dict[str, str] = {}
            for slug in pending_merge:
                task_row = get_dag_task(session_root, run_id, slug)
                if task_row and task_row.get("pane_slug"):
                    pane_slugs[slug] = task_row["pane_slug"]

            merged, failed_summary = _merge_ready_tasks(
                dag,
                dag_key,
                run_id,
                task_states,
                pending_merge,
                pane_slugs,
                session_root,
                all_merged,
            )
            all_merged.extend(merged)
            if failed_summary is not None:
                return failed_summary

            pending_merge = []

        # Check if we're done: no ready tasks, no active panes, nothing pending merge
        remaining_unprocessed = [
            slug
            for slug, state in task_states.items()
            if state not in ("merged", "reviewed_pass", "failed", "skipped", "")
        ]
        no_ready = not ready
        no_active = not active_panes

        if no_ready and no_active and not remaining_unprocessed:
            break

        if no_ready and no_active:
            cursor = latest_event_id(session_root)
            new_events = _wait_for_dag_progress_event(
                session_root,
                run_id,
                tuple(sorted(dag.tasks)),
                after_id=cursor,
            )
            if not new_events:
                update_dag_run(session_root, run_id, status="failed")
                emit_event(
                    session_root,
                    "dag_failed",
                    f"dag/{run_id}",
                    dag_run_id=run_id,
                    error="stalled",
                )
                return _build_summary(run_id, dag_key, "failed", task_states, all_merged, dag)

            task_rows = list_dag_tasks(session_root, run_id)
            task_states = {r["slug"]: r["status"] for r in task_rows}

    # Finalize status
    tiers = compute_tiers(dag.tasks)
    max_tier = tier_limit if tier_limit is not None else len(tiers) - 1

    if tier_limit is not None and highest_tier_started < max_tier:
        unexecuted = [
            s
            for s in dag.tasks
            if task_states.get(s) not in ("merged", "reviewed_pass", "failed", "skipped")
        ]
        if unexecuted:
            final_status = "partial"
            update_dag_run(session_root, run_id, status=final_status)
            emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)
            return _build_summary(run_id, dag_key, final_status, task_states, all_merged, dag)

    if not auto_merge:
        if all_merged or any(st == "reviewed_pass" for st in task_states.values()):
            final_status = "awaiting_merge"
        else:
            final_status = "failed"
    elif all_failed and not all_merged:
        final_status = "failed"
    else:
        final_status = "completed"

    _progress(f"DAG {final_status}: {len(all_merged)} merged, {len(all_failed)} failed")
    update_dag_run(session_root, run_id, status=final_status)
    emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)
    return _build_summary(run_id, dag_key, final_status, task_states, all_merged, dag)


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
    """Execute a DAG file using readiness-based scheduling.

    Tasks become runnable when all dependencies are in terminal-success state:
    - merged (if auto_merge=True)
    - reviewed_pass or merged (if auto_merge=False)

    Does not block ready work behind tier barriers. Merges happen in
    topological order separately from execution.
    """
    dag = parse_dag_file(dag_file)
    if dry_run:
        tiers = compute_tiers(dag.tasks)
        print(render_dry_run(tiers, dag.tasks))
        return DagRunSummary(run_id=0, dag_file=dag_file, status="dry_run")
    return run_dag_definition(
        dag,
        dag_key=str(Path(dag_file).resolve()),
        definition_hash=_dag_file_hash(dag_file),
        dry_run=False,
        tier_limit=tier_limit,
        skip=skip,
        max_retries=max_retries,
        auto_merge=auto_merge,
        max_concurrent=max_concurrent,
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


# Retry and escalation logic
# ---------------------------------------------------------------------------


def _augment_prompt_with_review(
    original_prompt: str,
    review_result: dict | None,
    pane_slug: str,
    session_root: str,
) -> str:
    """Prepend review feedback and log tail to the original prompt."""
    parts = ["The previous attempt failed. Issues found:\n"]

    if review_result and review_result.get("issues"):
        for issue in review_result["issues"]:
            parts.append(f"  - {issue}")

    # Try to get log tail context
    try:
        from dgov.recovery import retry_context

        ctx = retry_context(pane_slug, session_root)
        if ctx:
            parts.append(f"\nLog tail:\n{ctx}")
    except Exception:
        pass

    parts.append(f"\n---\n{original_prompt}")
    return "\n".join(parts)


def _task_failure_reason(
    wait_result: dict | Exception | None,
    review_result: dict | None,
) -> str:
    """Classify the failure reason for event emission."""
    if isinstance(wait_result, Exception):
        exc_name = type(wait_result).__name__
        if "Timeout" in exc_name:
            return "timeout"
        return "runtime_failed"
    if wait_result and not wait_result.get("ok"):
        error = wait_result.get("error", "")
        if "health" in error.lower():
            return "health_check_failed"
        if "timeout" in error.lower():
            return "timeout"
        return "runtime_failed"
    if review_result:
        if review_result.get("commit_count", 1) == 0:
            return "zero_commit"
        if not review_result.get("passed"):
            return "review_failed"
    return "runtime_failed"


def run_task_until_terminal(
    dag: DagDefinition,
    task: DagTaskSpec,
    run_id: int,
    max_retries: int,
    session_root: str,
) -> dict:
    """Run a single task through retry and escalation until terminal state.

    Returns dict with keys: status, agent, attempt, reason.
    """
    from dgov.persistence import emit_event, upsert_dag_task

    chain = [task.agent] + list(task.escalation)

    for agent_idx_try in range(len(chain)):
        current_agent = chain[agent_idx_try]

        for attempt_try in range(max_retries + 1):
            # Dispatch
            try:
                pane_info = _dispatch_task(dag, task, run_id, session_root)
                pane_slug = pane_info["pane_slug"]
            except Exception as exc:
                reason = _task_failure_reason(exc, None)
                next_ag = chain[agent_idx_try + 1] if agent_idx_try + 1 < len(chain) else "none"
                emit_event(
                    session_root,
                    "dag_task_escalated",
                    task.slug,
                    dag_run_id=run_id,
                    reason=reason,
                    from_agent=current_agent,
                    to_agent=next_ag,
                )
                break  # try next agent

            # Wait
            lifecycle = run_post_dispatch_lifecycle(
                dag.project_root,
                pane_slug,
                session_root=session_root,
                timeout=task.timeout_s,
                max_retries=0,
                auto_merge=False,
            )
            review = lifecycle.review or {}
            commit_count = int(review.get("commit_count", 0))
            task_states: dict[str, str] = {}
            status = _persist_task_lifecycle_result(
                dag,
                task.slug,
                lifecycle,
                session_root,
                run_id,
                task_states,
            )

            if status == "reviewed_pass":
                return {
                    "status": "reviewed_pass",
                    "agent": current_agent,
                    "attempt": attempt_try + 1,
                    "pane_slug": lifecycle.slug,
                }

            if status == "reviewed_fail":
                if commit_count == 0:
                    emit_event(
                        session_root,
                        "dag_task_escalated",
                        task.slug,
                        dag_run_id=run_id,
                        reason="zero_commit",
                    )
                    break
                if attempt_try < max_retries:
                    continue
                break

            if lifecycle.failure_stage == "timeout":
                emit_event(
                    session_root,
                    "dag_task_escalated",
                    task.slug,
                    dag_run_id=run_id,
                    reason="timeout",
                )
                break
            if commit_count == 0:
                emit_event(
                    session_root,
                    "dag_task_escalated",
                    task.slug,
                    dag_run_id=run_id,
                    reason="zero_commit",
                )
                break
            if lifecycle.failure_stage == "worker_failed":
                if attempt_try < max_retries:
                    continue
                break
            if attempt_try < max_retries:
                continue
            break

    # All agents exhausted
    upsert_dag_task(
        session_root,
        run_id,
        task.slug,
        "failed",
        chain[-1],
        error="all agents exhausted",
    )
    emit_event(
        session_root,
        "dag_task_failed",
        task.slug,
        dag_run_id=run_id,
        reason="exhausted",
    )
    return {"status": "failed", "agent": chain[-1], "attempt": 0, "reason": "exhausted"}
