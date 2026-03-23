"""Batch execution and checkpoint management."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from dgov.dag import DagDefinition, DagRunSummary, run_dag_via_kernel
from dgov.dag_graph import (
    compute_tiers as _dag_compute_tiers,
)
from dgov.dag_graph import (
    transitive_dependents as _dag_transitive_dependents,
)
from dgov.dag_parser import DagFileSpec, DagTaskSpec
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    emit_event,
)


def create_checkpoint(
    project_root: str,
    name: str,
    session_root: str | None = None,
) -> dict:
    """Create a checkpoint snapshot of current state."""
    from datetime import datetime, timezone

    session_root = os.path.abspath(session_root or project_root)

    # Get main SHA
    main_sha_result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    main_sha = main_sha_result.stdout.strip() if main_sha_result.returncode == 0 else ""

    # Get all pane records
    panes = all_panes(session_root)

    # Get branch heads for each pane
    branch_heads = {}
    for p in panes:
        branch = p.get("branch_name", "")
        wt = p.get("worktree_path", "")
        if branch and wt and Path(wt).exists():
            head_result = subprocess.run(
                ["git", "-C", wt, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if head_result.returncode == 0:
                branch_heads[branch] = head_result.stdout.strip()

    checkpoint = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "main_sha": main_sha,
        "panes": panes,
        "branch_heads": branch_heads,
    }

    # Write to .dgov/checkpoints/<name>.json
    checkpoint_dir = Path(session_root) / STATE_DIR / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"{name}.json"

    overwrote = None
    if checkpoint_path.exists():
        existing = json.loads(checkpoint_path.read_text())
        overwrote = existing.get("ts", "unknown")

    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint, f, indent=2, default=str)
        f.write("\n")

    emit_event(session_root, "checkpoint_created", f"checkpoint/{name}", main_sha=main_sha)

    result = {"checkpoint": name, "main_sha": main_sha, "pane_count": len(panes)}
    if overwrote:
        result["overwrote"] = overwrote
    return result


def list_checkpoints(session_root: str) -> list[dict]:
    """List all checkpoints."""
    checkpoint_dir = Path(session_root) / STATE_DIR / "checkpoints"
    if not checkpoint_dir.exists():
        return []

    checkpoints = []
    for f in sorted(checkpoint_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            checkpoints.append(
                {
                    "name": data.get("name", f.stem),
                    "ts": data.get("ts", ""),
                    "pane_count": len(data.get("panes", [])),
                    "main_sha": data.get("main_sha", "")[:8],
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return checkpoints


def restore_checkpoint(session_root: str, name: str) -> dict:
    """Load a previously created checkpoint by name."""
    checkpoint_path = Path(session_root) / STATE_DIR / "checkpoints" / f"{name}.json"
    with open(checkpoint_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Batch spec parsing
# ---------------------------------------------------------------------------


def _parse_spec(spec_path: str) -> tuple[str, dict[str, dict]]:
    """Parse a batch spec file (TOML or legacy JSON).

    Returns (project_root, tasks_dict) where tasks_dict maps task ID to task fields.
    """
    import tomllib

    from dgov.agents import get_default_agent

    path = Path(spec_path)
    suffix = path.suffix.lower()

    if suffix == ".toml":
        with open(path, "rb") as f:
            spec = tomllib.load(f)
        project_root = spec.get("project_root", ".")
        raw_tasks = spec.get("tasks", {})
        default_agent = get_default_agent()
        tasks: dict[str, dict] = {}
        for task_id, fields in raw_tasks.items():
            if "prompt" not in fields:
                raise ValueError(f"Task '{task_id}' missing required field 'prompt'")
            tasks[task_id] = {
                "id": task_id,
                "prompt": fields["prompt"],
                "agent": fields.get("agent", default_agent),
                "touches": fields.get("touches", []),
                "depends_on": fields.get("depends_on", []),
                "timeout": fields.get("timeout", 600),
                "permission_mode": fields.get("permission_mode", "bypassPermissions"),
            }
    else:
        with open(path) as f:
            spec = json.load(f)
        project_root = spec["project_root"]
        tasks = {}
        for t in spec["tasks"]:
            task_id = t["id"]
            tasks[task_id] = {
                "id": task_id,
                "prompt": t["prompt"],
                "agent": t.get("agent", "claude"),
                "touches": t.get("touches", []),
                "depends_on": t.get("depends_on", []),
                "timeout": t.get("timeout", 600),
                "permission_mode": t.get("permission_mode", "bypassPermissions"),
            }

    return project_root, tasks


# ---------------------------------------------------------------------------
# DAG validation and tier computation (delegated to dag module)
# ---------------------------------------------------------------------------


def _task_dict_to_spec(task_id: str, task: dict) -> DagTaskSpec:
    """Convert batch-style task dict to DagTaskSpec for shared helpers."""
    touches = task.get("touches", [])
    return DagTaskSpec(
        slug=task_id,
        summary=task.get("prompt", "")[:80],
        prompt=task.get("prompt", ""),
        commit_message="",
        agent=task.get("agent", "claude"),
        escalation=(),
        depends_on=tuple(task.get("depends_on", ())),
        files=DagFileSpec(edit=tuple(sorted(touches))),
        permission_mode=task.get("permission_mode", "bypassPermissions"),
        timeout_s=task.get("timeout", 600),
    )


def _to_dag_specs(tasks: dict[str, dict]) -> dict[str, DagTaskSpec]:
    """Convert all batch tasks to DagTaskSpec."""
    return {tid: _task_dict_to_spec(tid, t) for tid, t in tasks.items()}


def _compute_tiers(tasks: dict[str, dict]) -> list[list[dict]]:
    """Group tasks into parallel tiers respecting depends_on and touch overlap."""
    specs = _to_dag_specs(tasks)
    tier_slugs = _dag_compute_tiers(specs)
    return [[tasks[slug] for slug in tier] for tier in tier_slugs]


def _transitive_dependents(tasks: dict[str, dict], failed_ids: set[str]) -> set[str]:
    """Return all task IDs that transitively depend on any of the failed_ids."""
    return _dag_transitive_dependents(_to_dag_specs(tasks), failed_ids)


def _render_dry_run(tiers: list[list[dict]], tasks: dict[str, dict]) -> str:
    """Render a tier listing with box-drawing characters."""
    total = sum(len(t) for t in tiers)
    lines = [f"DAG ({total} tasks, {len(tiers)} tiers):", ""]
    for i, tier in enumerate(tiers):
        ids = ", ".join(t["id"] for t in tier)
        lines.append(f"  Tier {i}: {ids}")
    return "\n".join(lines)


def _spec_hash(spec_path: str) -> str:
    """SHA-256 of the raw batch spec bytes."""
    return hashlib.sha256(Path(spec_path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------
def _batch_to_dag_definition(
    spec_path: str,
    project_root: str,
    session_root: str,
    tasks: dict[str, dict],
) -> DagDefinition:
    """Compile a batch spec into a DAG definition for canonical execution."""
    return DagDefinition(
        name=Path(spec_path).stem,
        dag_file=str(Path(spec_path).resolve()),
        project_root=project_root,
        session_root=session_root,
        default_max_retries=0,
        merge_resolve="skip",
        merge_squash=True,
        max_concurrent=0,
        tasks=_to_dag_specs(tasks),
    )


def _build_batch_tier_results(
    tiers: list[list[dict]],
    task_rows: list[dict],
) -> list[dict]:
    """Reconstruct batch tier output from canonical DAG task records."""
    rows_by_slug = {row["slug"]: row for row in task_rows}
    tier_results: list[dict] = []
    for tier_idx, tier in enumerate(tiers):
        tasks_out: list[dict] = []
        for task in tier:
            row = rows_by_slug.get(task["id"])
            if row is None:
                tasks_out.append({"id": task["id"], "status": "skipped"})
                continue
            status = "review_pending" if row["status"] == "reviewed_fail" else row["status"]
            record = {
                "id": task["id"],
                "status": status,
            }
            if row.get("pane_slug"):
                record["slug"] = row["pane_slug"]
            if row.get("error"):
                record["error"] = row["error"]
            tasks_out.append(record)
        tier_results.append({"tier": tier_idx, "tasks": tasks_out})
    return tier_results


def _dag_summary_to_batch_result(
    summary: DagRunSummary,
    tiers: list[list[dict]],
    task_rows: list[dict],
) -> dict:
    """Translate canonical DAG output into the legacy batch result shape."""
    return {
        "tiers": _build_batch_tier_results(tiers, task_rows),
        "merged": list(summary.merged),
        "failed": list(summary.failed),
        "skipped": list(summary.skipped),
    }


def run_batch(
    spec_path: str,
    session_root: str | None = None,
    dry_run: bool = False,
    project_root: str | None = None,
) -> dict:
    """Execute a batch spec by compiling it into the canonical DAG scheduler.

    Supports TOML (.toml) and legacy JSON (.json) spec formats.
    Tasks are ordered by explicit depends_on and implicit touch overlap.
    On failure, transitive dependents are skipped; unrelated branches continue.
    """
    from dgov.cli.pane import _autocorrect_roots
    from dgov.persistence import list_dag_tasks

    spec_project_root, tasks = _parse_spec(spec_path)
    if project_root is None:
        # Use spec file's project_root (or default ".")
        project_root = spec_project_root
    else:
        # CLI-provided project_root overrides spec file
        pass

    project_root, _ = _autocorrect_roots(project_root, session_root)
    session_root = os.path.abspath(session_root or project_root)

    tiers = _compute_tiers(tasks)

    if dry_run:
        ascii_dag = _render_dry_run(tiers, tasks)
        return {
            "dry_run": True,
            "tiers": [[t["id"] for t in tier] for tier in tiers],
            "total_tasks": len(tasks),
            "ascii_dag": ascii_dag,
        }

    dag = _batch_to_dag_definition(spec_path, project_root, session_root, tasks)
    submission = run_dag_via_kernel(
        dag,
        dag_key=str(Path(spec_path).resolve()),
        definition_hash=_spec_hash(spec_path),
        auto_merge=True,
    )
    run_id = submission.run_id

    # Headless migration: run_dag_via_kernel is now non-blocking.
    # To satisfy legacy synchronous callers (CLI), we must wait here.
    from dgov.dag_parser import DagRunSummary
    from dgov.persistence import get_dag_run, latest_event_id, wait_for_events

    cursor = latest_event_id(session_root)
    while True:
        events = wait_for_events(
            session_root,
            after_id=cursor,
            event_types=("dag_completed", "dag_failed"),
            timeout_s=60.0,
        )
        finished = False
        for ev in events:
            cursor = max(cursor, ev["id"])
            data = json.loads(ev["data"])
            if data.get("dag_run_id") == run_id:
                finished = True
                break

        if finished:
            break

        # Safety: check if it already finished between submission and wait
        run = get_dag_run(session_root, run_id)
        if run and run["status"] in ("completed", "failed", "partial", "cancelled"):
            break

    # Re-fetch final state
    run = get_dag_run(session_root, run_id)
    task_states = run["state_json"].get("task_states", {})
    final_summary = DagRunSummary(
        run_id=run_id,
        dag_file=submission.dag_file,
        status=run["status"],
        merged=[s for s, st in task_states.items() if st == "merged"],
        failed=[s for s, st in task_states.items() if st == "failed"],
        skipped=[s for s, st in task_states.items() if st == "skipped"],
        blocked=[s for s, st in task_states.items() if st == "blocked_on_governor"],
    )

    task_rows = list_dag_tasks(session_root, run_id)
    return _dag_summary_to_batch_result(final_summary, tiers, task_rows)


def batch_dispatch(
    spec_path: str,
    session_root: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Compatibility wrapper around run_batch used by older callers."""
    return run_batch(spec_path, session_root=session_root, dry_run=dry_run)
