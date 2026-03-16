"""DAG file parser and execution engine for dgov."""

from __future__ import annotations

import hashlib
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DagFileSpec:
    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagTaskSpec:
    slug: str
    summary: str
    prompt: str
    commit_message: str
    agent: str
    escalation: tuple[str, ...]
    depends_on: tuple[str, ...]
    files: DagFileSpec
    permission_mode: str
    timeout_s: int


@dataclass(frozen=True)
class DagDefinition:
    name: str
    dag_file: str
    project_root: str
    session_root: str
    default_max_retries: int
    merge_resolve: str
    merge_squash: bool
    tasks: dict[str, DagTaskSpec]


@dataclass(frozen=True)
class DagRunOptions:
    dry_run: bool = False
    tier_limit: int | None = None
    skip: frozenset[str] = frozenset()
    max_retries: int = 1
    auto_merge: bool = True


@dataclass
class DagRunSummary:
    run_id: int
    dag_file: str
    status: str
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalated: list[dict[str, object]] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    unmerged: list[str] = field(default_factory=list)


def parse_dag_file(path: str) -> DagDefinition:
    """Parse a TOML DAG file into a DagDefinition."""
    raw_bytes = Path(path).read_bytes()
    raw = tomllib.loads(raw_bytes.decode())
    dag_section = raw.get("dag")
    if not dag_section:
        raise ValueError("Missing [dag] section")
    if "version" not in dag_section:
        raise ValueError("Missing dag.version")
    if "name" not in dag_section:
        raise ValueError("Missing dag.name")
    tasks_raw = raw.get("tasks")
    if not tasks_raw:
        raise ValueError("Missing [tasks] section")

    project_root = dag_section.get("project_root", ".")
    session_root = dag_section.get("session_root", ".")
    defaults = {
        "permission_mode": dag_section.get("default_permission_mode", "acceptEdits"),
        "timeout_s": dag_section.get("default_timeout_s", 900),
    }

    tasks: dict[str, DagTaskSpec] = {}
    for slug, task_raw in tasks_raw.items():
        tasks[slug] = _parse_task(slug, task_raw, defaults, path, project_root, session_root)

    return DagDefinition(
        name=dag_section["name"],
        dag_file=str(Path(path).resolve()),
        project_root=project_root,
        session_root=session_root,
        default_max_retries=dag_section.get("default_max_retries", 1),
        merge_resolve=dag_section.get("merge_resolve", "skip"),
        merge_squash=dag_section.get("merge_squash", True),
        tasks=tasks,
    )


def _parse_task(
    slug: str,
    raw: dict,
    defaults: dict,
    dag_file: str,
    project_root: str,
    session_root: str,
) -> DagTaskSpec:
    """Parse and validate a single task block."""
    for req in ("summary", "agent", "prompt", "commit_message"):
        if not raw.get(req):
            raise ValueError(f"Task {slug!r}: missing required field {req!r}")
    if not raw.get("prompt", "").strip():
        raise ValueError(f"Task {slug!r}: prompt must not be empty")

    files_raw = raw.get("files", {})
    files = _normalize_file_specs(project_root, files_raw)
    if not files.create and not files.edit and not files.delete:
        raise ValueError(f"Task {slug!r}: must specify at least one file in create/edit/delete")

    return DagTaskSpec(
        slug=slug,
        summary=raw["summary"],
        prompt=raw["prompt"],
        commit_message=raw["commit_message"],
        agent=raw["agent"],
        escalation=tuple(raw.get("escalation", ())),
        depends_on=tuple(raw.get("depends_on", ())),
        files=files,
        permission_mode=raw.get("permission_mode", defaults["permission_mode"]),
        timeout_s=raw.get("timeout_s", defaults["timeout_s"]),
    )


def _normalize_file_specs(project_root: str, files: dict) -> DagFileSpec:
    """Normalize file spec lists: validate no globs, all relative, sort."""
    result = {}
    for key in ("create", "edit", "delete"):
        paths = files.get(key, [])
        for p in paths:
            if "*" in p or "?" in p or "[" in p:
                raise ValueError(f"Globs not allowed in file specs: {p!r}")
            if Path(p).is_absolute():
                raise ValueError(f"File paths must be relative: {p!r}")
        result[key] = tuple(sorted(paths))
    return DagFileSpec(**result)


def validate_dag(tasks: dict[str, DagTaskSpec]) -> None:
    """Validate depends_on references exist and there are no cycles."""
    task_ids = set(tasks)
    for slug, task in tasks.items():
        for dep in task.depends_on:
            if dep not in task_ids:
                raise ValueError(f"Task {slug!r} depends on {dep!r} which does not exist")

    visited: set[str] = set()
    path: set[str] = set()

    def _visit(node: str) -> None:
        if node in path:
            raise ValueError(f"Dependency cycle detected involving {node!r}")
        if node in visited:
            return
        path.add(node)
        for dep in tasks[node].depends_on:
            _visit(dep)
        path.discard(node)
        visited.add(node)

    for tid in tasks:
        _visit(tid)


def topological_order(tasks: dict[str, DagTaskSpec]) -> list[str]:
    """Return task slugs in stable topological order."""
    validate_dag(tasks)
    visited: set[str] = set()
    order: list[str] = []

    def _visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(tasks[node].depends_on):
            _visit(dep)
        order.append(node)

    for tid in sorted(tasks):
        _visit(tid)
    return order


def _touches(task: DagTaskSpec) -> set[str]:
    """Return the union of all file specs for overlap checking."""
    return set(task.files.create) | set(task.files.edit) | set(task.files.delete)


def _paths_overlap(a: str, b: str) -> bool:
    """True if paths conflict: exact match, or ancestor/descendant."""
    if a == b:
        return True
    a_clean = a.rstrip("/")
    b_clean = b.rstrip("/")
    return a_clean.startswith(b_clean + "/") or b_clean.startswith(a_clean + "/")


def compute_tiers(tasks: dict[str, DagTaskSpec]) -> list[list[str]]:
    """Group tasks into parallel tiers respecting deps and file overlap."""
    validate_dag(tasks)
    placed: dict[str, int] = {}
    tiers: list[list[str]] = []
    remaining = set(tasks)

    while remaining:
        tier: list[str] = []
        tier_touches: set[str] = set()
        placed_this_round: list[str] = []

        for slug in sorted(remaining):
            task = tasks[slug]
            if not all(d in placed for d in task.depends_on):
                continue
            task_files = _touches(task)
            has_overlap = False
            for tf in task_files:
                for et in tier_touches:
                    if _paths_overlap(tf, et):
                        has_overlap = True
                        break
                if has_overlap:
                    break
            if has_overlap:
                continue
            tier.append(slug)
            tier_touches.update(task_files)
            placed_this_round.append(slug)

        if not placed_this_round:
            raise ValueError(f"Cannot schedule remaining tasks: {remaining}")

        tier_idx = len(tiers)
        for slug in placed_this_round:
            placed[slug] = tier_idx
            remaining.discard(slug)
        tiers.append(tier)

    return tiers


def transitive_dependents(tasks: dict[str, DagTaskSpec], failed: set[str]) -> set[str]:
    """Return all task slugs that transitively depend on any failed slug."""
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for slug, task in tasks.items():
            if slug in dependents or slug in failed:
                continue
            if any(d in failed or d in dependents for d in task.depends_on):
                dependents.add(slug)
                changed = True
    return dependents


def render_dry_run(tiers: list[list[str]], tasks: dict[str, DagTaskSpec]) -> str:
    """Render a human-readable tier listing."""
    total = sum(len(t) for t in tiers)
    lines = [f"DAG ({total} tasks, {len(tiers)} tiers):", ""]
    for i, tier in enumerate(tiers):
        slugs = ", ".join(tier)
        lines.append(f"  Tier {i}: {slugs}")
        for slug in tier:
            task = tasks[slug]
            lines.append(f"    {slug}: {task.summary} [{task.agent}]")
    return "\n".join(lines)


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

    logger.info("Dispatching task %s with agent %s", task.slug, task.agent)
    pane = create_worker_pane(
        project_root=dag.project_root,
        prompt=task.prompt,
        agent=task.agent,
        permission_mode=task.permission_mode,
        slug=task.slug,
        session_root=session_root,
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
    """Wait for all active panes in a tier. Returns {task_slug: wait_result}."""
    from dgov.waiter import wait_worker_pane

    results: dict[str, dict] = {}
    for task_slug, pane_info in active_panes.items():
        pane_slug = pane_info["pane_slug"]
        task = dag.tasks[task_slug]
        try:
            result = wait_worker_pane(
                dag.project_root,
                pane_slug,
                session_root=session_root,
                timeout=task.timeout_s,
                auto_retry=False,
            )
            results[task_slug] = {"ok": True, "result": result, "pane_slug": pane_slug}
        except Exception as exc:
            logger.warning("Wait failed for %s: %s", task_slug, exc)
            results[task_slug] = {"ok": False, "error": str(exc), "pane_slug": pane_slug}
    return results


def _review_task(
    dag: DagDefinition,
    task_slug: str,
    pane_slug: str,
    session_root: str,
) -> dict:
    """Review a completed task. Returns review result dict."""
    from dgov.inspection import review_worker_pane
    from dgov.persistence import update_pane_state

    result = review_worker_pane(
        dag.project_root,
        pane_slug,
        session_root=session_root,
        full=False,
    )
    if result.get("error"):
        return result

    if result.get("verdict") == "safe":
        update_pane_state(session_root, pane_slug, "reviewed_pass", force=True)
        return {"passed": True, **result}
    else:
        update_pane_state(session_root, pane_slug, "reviewed_fail", force=True)
        return {"passed": False, **result}


def _merge_tasks_in_order(
    dag: DagDefinition,
    ready: list[str],
    pane_slugs: dict[str, str],
    session_root: str,
) -> tuple[list[str], dict | None]:
    """Merge reviewed-pass tasks in canonical order.

    Returns (merged_list, error_or_None).
    """
    from dgov.merger import merge_worker_pane
    from dgov.persistence import emit_event, upsert_dag_task

    topo = topological_order(dag.tasks)
    ordered = [s for s in topo if s in ready]
    merged: list[str] = []

    for task_slug in ordered:
        pane_slug = pane_slugs[task_slug]
        logger.info("Merging %s (pane %s)", task_slug, pane_slug)
        result = merge_worker_pane(
            dag.project_root,
            pane_slug,
            session_root=session_root,
            resolve=dag.merge_resolve,
            squash=dag.merge_squash,
        )
        if "error" in result:
            logger.error("Merge error for %s: %s", task_slug, result["error"])
            return merged, result

        merged.append(task_slug)
        upsert_dag_task(session_root, 0, task_slug, "merged", dag.tasks[task_slug].agent)
        emit_event(session_root, "dag_task_completed", task_slug, dag_run_id=0)

    return merged, None


def _get_pane_state(session_root: str, pane_slug: str) -> str | None:
    """Get the current state of a pane."""
    from dgov.persistence import get_pane

    pane = get_pane(session_root, pane_slug)
    if pane is None:
        return None
    return pane.get("state")


def run_single_tier(
    dag: DagDefinition,
    tier: list[str],
    run_id: int,
    task_states: dict[str, str],
    options: DagRunOptions,
    session_root: str,
) -> dict:
    """Execute one tier: dispatch, wait, review, merge.

    Returns dict with keys: reviewed_pass, failed, merged, merge_error.
    """
    from dgov.persistence import upsert_dag_task

    # Dispatch all ready tasks
    active_panes: dict[str, dict] = {}
    for slug in tier:
        if task_states.get(slug) in ("merged", "reviewed_pass", "skipped", "failed"):
            continue
        task = dag.tasks[slug]
        try:
            pane_info = _dispatch_task(dag, task, run_id, session_root)
            active_panes[slug] = pane_info
            task_states[slug] = "dispatched"
        except Exception as exc:
            logger.error("Dispatch failed for %s: %s", slug, exc)
            task_states[slug] = "failed"
            upsert_dag_task(session_root, run_id, slug, "failed", task.agent, error=str(exc))

    if not active_panes:
        return {"reviewed_pass": [], "failed": [], "merged": [], "merge_error": None}

    # Wait for all
    wait_results = _wait_for_tier(dag, active_panes, session_root)

    # Review completed tasks (only review panes in done state)
    reviewed_pass: list[str] = []
    reviewed_fail: list[str] = []
    pane_slugs: dict[str, str] = {}

    for task_slug, wait_res in wait_results.items():
        pane_slug = wait_res["pane_slug"]
        pane_slugs[task_slug] = pane_slug

        if not wait_res["ok"]:
            task_states[task_slug] = "failed"
            upsert_dag_task(
                session_root,
                run_id,
                task_slug,
                "failed",
                dag.tasks[task_slug].agent,
                error=wait_res.get("error"),
            )
            continue

        # Check pane state — only review if done
        pane_state = _get_pane_state(session_root, pane_slug)
        if pane_state in ("failed", "timed_out", "abandoned"):
            task_states[task_slug] = "failed"
            upsert_dag_task(
                session_root,
                run_id,
                task_slug,
                "failed",
                dag.tasks[task_slug].agent,
                error=f"pane ended in {pane_state}",
            )
            continue

        review = _review_task(dag, task_slug, pane_slug, session_root)
        if review.get("error"):
            task_states[task_slug] = "failed"
            upsert_dag_task(
                session_root,
                run_id,
                task_slug,
                "failed",
                dag.tasks[task_slug].agent,
                error=review["error"],
            )
            continue

        if review.get("passed"):
            reviewed_pass.append(task_slug)
            task_states[task_slug] = "reviewed_pass"
            upsert_dag_task(
                session_root,
                run_id,
                task_slug,
                "reviewed_pass",
                dag.tasks[task_slug].agent,
                pane_slug=pane_slug,
            )
        else:
            reviewed_fail.append(task_slug)
            task_states[task_slug] = "reviewed_fail"
            upsert_dag_task(
                session_root,
                run_id,
                task_slug,
                "reviewed_fail",
                dag.tasks[task_slug].agent,
                pane_slug=pane_slug,
            )

    # Merge if auto_merge
    merged: list[str] = []
    merge_error: dict | None = None
    if options.auto_merge and reviewed_pass:
        merged, merge_error = _merge_tasks_in_order(dag, reviewed_pass, pane_slugs, session_root)
        for slug in merged:
            task_states[slug] = "merged"

    failed = [s for s in tier if task_states.get(s) in ("failed", "reviewed_fail")]
    return {
        "reviewed_pass": reviewed_pass,
        "failed": failed,
        "merged": merged,
        "merge_error": merge_error,
    }


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
    from datetime import datetime, timezone

    from dgov.persistence import (
        create_dag_run,
        emit_event,
        ensure_dag_tables,
        get_open_dag_run,
        list_dag_tasks,
    )

    abs_path = str(Path(dag_file).resolve())
    session_root = os.path.abspath(session_root)
    ensure_dag_tables(session_root)

    dag = parse_dag_file(dag_file)
    file_hash = _dag_file_hash(dag_file)

    existing = get_open_dag_run(session_root, abs_path)
    if existing:
        stored_hash = existing.get("state_json", {}).get("dag_sha256", "")
        if stored_hash and stored_hash != file_hash:
            raise ValueError(
                f"DAG file has changed since run {existing['id']} started. "
                f"Stored hash: {stored_hash[:12]}..., current: {file_hash[:12]}..."
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
        "dag_sha256": file_hash,
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
        abs_path,
        datetime.now(timezone.utc).isoformat(),
        "running",
        0,
        state_json,
    )
    emit_event(session_root, "dag_started", f"dag/{run_id}", dag_run_id=run_id)
    logger.info("Started new DAG run %d for %s", run_id, dag.name)
    return run_id, dag, {}


def run_dag(
    dag_file: str,
    *,
    dry_run: bool = False,
    tier_limit: int | None = None,
    skip: set[str] | None = None,
    max_retries: int = 1,
    auto_merge: bool = True,
) -> DagRunSummary:
    """Execute a TOML DAG: dispatch, wait, review, merge per tier."""
    from dgov.lifecycle import close_worker_pane
    from dgov.persistence import emit_event, update_dag_run, upsert_dag_task

    dag = parse_dag_file(dag_file)
    options = DagRunOptions(
        dry_run=dry_run,
        tier_limit=tier_limit,
        skip=frozenset(skip or ()),
        max_retries=max_retries,
        auto_merge=auto_merge,
    )

    if dry_run:
        tiers = compute_tiers(dag.tasks)
        print(render_dry_run(tiers, dag.tasks))
        return DagRunSummary(run_id=0, dag_file=dag_file, status="dry_run")

    session_root = os.path.abspath(dag.session_root)
    run_id, dag, task_states = _start_or_resume_run(dag_file, options, session_root)
    tiers = compute_tiers(dag.tasks)

    # Apply skip + transitive
    skipped = set(options.skip)
    if skipped:
        transitive = transitive_dependents(dag.tasks, skipped)
        skipped |= transitive
        for slug in skipped:
            task_states[slug] = "skipped"
            upsert_dag_task(session_root, run_id, slug, "skipped", dag.tasks[slug].agent)
            # Close already-dispatched panes for newly-skipped tasks
            existing = [
                t for t in (list(task_states.keys())) if task_states.get(t) == "dispatched"
            ]
            for s in existing:
                if s in skipped:
                    try:
                        close_worker_pane(
                            dag.project_root, s, session_root=session_root, force=True
                        )
                    except Exception:
                        pass

    max_tier = tier_limit if tier_limit is not None else len(tiers) - 1
    all_merged: list[str] = []
    all_failed: list[str] = []

    for tier_idx, tier in enumerate(tiers):
        if tier_idx > max_tier:
            break

        # Filter out skipped tasks
        active_tier = [s for s in tier if task_states.get(s) != "skipped"]
        if not active_tier:
            continue

        update_dag_run(session_root, run_id, current_tier=tier_idx)
        emit_event(
            session_root, "dag_tier_started", f"dag/{run_id}", dag_run_id=run_id, tier=tier_idx
        )

        result = run_single_tier(dag, active_tier, run_id, task_states, options, session_root)

        all_merged.extend(result["merged"])
        all_failed.extend(result["failed"])

        emit_event(
            session_root, "dag_tier_completed", f"dag/{run_id}", dag_run_id=run_id, tier=tier_idx
        )

        if result["merge_error"]:
            update_dag_run(session_root, run_id, status="failed")
            emit_event(
                session_root,
                "dag_failed",
                f"dag/{run_id}",
                dag_run_id=run_id,
                error="merge_conflict",
            )
            return _build_summary(run_id, dag_file, "failed", task_states, all_merged, dag)

        # Transitively skip dependents of failed tasks
        if result["failed"]:
            newly_skipped = transitive_dependents(dag.tasks, set(result["failed"]))
            for slug in newly_skipped:
                if task_states.get(slug) not in ("merged", "reviewed_pass", "failed", "skipped"):
                    task_states[slug] = "skipped"
                    upsert_dag_task(session_root, run_id, slug, "skipped", dag.tasks[slug].agent)

    # Finalize
    final_status = "awaiting_merge" if not auto_merge else "completed"
    if all_failed:
        final_status = "completed"  # partial success
    update_dag_run(session_root, run_id, status=final_status)
    emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)
    return _build_summary(run_id, dag_file, final_status, task_states, all_merged, dag)


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
    from dgov.merger import merge_worker_pane
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

    topo = topological_order(dag.tasks)
    ordered = [s for s in topo if s in ready]

    merged: list[str] = []
    for slug in ordered:
        pane_slug = pane_slugs.get(slug)
        if not pane_slug:
            continue
        result = merge_worker_pane(
            dag.project_root,
            pane_slug,
            session_root=session_root,
            resolve=dag.merge_resolve,
            squash=dag.merge_squash,
        )
        if "error" in result:
            update_dag_run(session_root, run_id, status="failed")
            emit_event(session_root, "dag_failed", f"dag/{run_id}", dag_run_id=run_id)
            return _build_summary(run_id, dag_file, "failed", task_states, merged, dag)
        merged.append(slug)
        task_states[slug] = "merged"
        upsert_dag_task(session_root, run_id, slug, "merged", dag.tasks[slug].agent)

    update_dag_run(session_root, run_id, status="completed")
    emit_event(session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id)
    return _build_summary(run_id, dag_file, "completed", task_states, merged, dag)
