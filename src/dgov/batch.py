"""Batch execution and checkpoint management."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from dgov.persistence import (
    _STATE_DIR,
    _all_panes,
    _emit_event,
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
    panes = _all_panes(session_root)

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
    checkpoint_dir = Path(session_root) / _STATE_DIR / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"{name}.json"

    overwrote = None
    if checkpoint_path.exists():
        existing = json.loads(checkpoint_path.read_text())
        overwrote = existing.get("ts", "unknown")

    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint, f, indent=2, default=str)
        f.write("\n")

    _emit_event(session_root, "checkpoint_created", f"checkpoint/{name}", main_sha=main_sha)

    result = {"checkpoint": name, "main_sha": main_sha, "pane_count": len(panes)}
    if overwrote:
        result["overwrote"] = overwrote
    return result


def list_checkpoints(session_root: str) -> list[dict]:
    """List all checkpoints."""
    checkpoint_dir = Path(session_root) / _STATE_DIR / "checkpoints"
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
                "permission_mode": fields.get("permission_mode", "acceptEdits"),
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
                "permission_mode": t.get("permission_mode", "acceptEdits"),
            }

    return project_root, tasks


# ---------------------------------------------------------------------------
# DAG validation and tier computation
# ---------------------------------------------------------------------------


def _validate_dag(tasks: dict[str, dict]) -> None:
    """Validate that depends_on references exist and there are no cycles."""
    task_ids = set(tasks)

    for task_id, task in tasks.items():
        for dep in task["depends_on"]:
            if dep not in task_ids:
                raise ValueError(f"Task '{task_id}' depends on '{dep}' which does not exist")

    # Topological sort to detect cycles
    visited: set[str] = set()
    path: set[str] = set()

    def _visit(node: str) -> None:
        if node in path:
            cycle = [node]
            raise ValueError(f"Dependency cycle detected: {' -> '.join(cycle)}")
        if node in visited:
            return
        path.add(node)
        for dep in tasks[node]["depends_on"]:
            _visit(dep)
        path.discard(node)
        visited.add(node)

    for tid in tasks:
        _visit(tid)


def _compute_tiers(tasks: dict[str, dict]) -> list[list[dict]]:
    """Group tasks into parallel tiers respecting depends_on and touch overlap.

    A task is ready for a tier when:
    1. All its depends_on are in earlier tiers
    2. Its touches don't overlap with any task in the same tier
    """
    _validate_dag(tasks)

    placed: dict[str, int] = {}  # task_id -> tier_index
    tiers: list[list[dict]] = []
    remaining = set(tasks)

    while remaining:
        tier: list[dict] = []
        tier_touches: set[str] = set()
        placed_this_round: list[str] = []

        for task_id in sorted(remaining):
            task = tasks[task_id]
            deps = task["depends_on"]

            # All deps must be placed in earlier tiers
            if not all(d in placed for d in deps):
                continue

            # Check touch overlap with tasks already in this tier
            task_touches = task.get("touches", [])
            has_overlap = False
            for tt in task_touches:
                for et in tier_touches:
                    if tt == et or tt.startswith(et) or et.startswith(tt):
                        has_overlap = True
                        break
                if has_overlap:
                    break

            if has_overlap:
                continue

            tier.append(task)
            tier_touches.update(task_touches)
            placed_this_round.append(task_id)

        if not placed_this_round:
            # Should not happen after validation, but guard against infinite loop
            raise ValueError(f"Cannot schedule remaining tasks: {remaining}")

        tier_idx = len(tiers)
        for tid in placed_this_round:
            placed[tid] = tier_idx
            remaining.discard(tid)
        tiers.append(tier)

    return tiers


def _transitive_dependents(tasks: dict[str, dict], failed_ids: set[str]) -> set[str]:
    """Return all task IDs that transitively depend on any of the failed_ids."""
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for tid, task in tasks.items():
            if tid in dependents or tid in failed_ids:
                continue
            if any(d in failed_ids or d in dependents for d in task["depends_on"]):
                dependents.add(tid)
                changed = True
    return dependents


def _render_dry_run(tiers: list[list[dict]], tasks: dict[str, dict]) -> str:
    """Render a tier listing with box-drawing characters."""
    total = sum(len(t) for t in tiers)
    lines = [f"DAG ({total} tasks, {len(tiers)} tiers):", ""]
    for i, tier in enumerate(tiers):
        ids = ", ".join(t["id"] for t in tier)
        lines.append(f"  Tier {i}: {ids}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


def run_batch(
    spec_path: str,
    session_root: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a batch spec: create panes, wait, merge in tier order.

    Supports TOML (.toml) and legacy JSON (.json) spec formats.
    Tasks are ordered by explicit depends_on and implicit touch overlap.
    On failure, transitive dependents are skipped; unrelated branches continue.
    """
    import dgov.panes as _p
    from dgov.merger import merge_worker_pane

    project_root, tasks = _parse_spec(spec_path)
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

    failed_ids: set[str] = set()
    skipped_ids: set[str] = set()
    results: dict = {"tiers": [], "merged": [], "failed": [], "skipped": []}

    for tier_idx, tier in enumerate(tiers):
        tier_result: dict = {"tier": tier_idx, "tasks": []}

        # Create all panes in this tier (skip tasks whose deps failed)
        slugs = []
        for task in tier:
            if task["id"] in skipped_ids:
                tier_result["tasks"].append({"id": task["id"], "status": "skipped"})
                results["skipped"].append(task["id"])
                continue

            try:
                pane = _p.create_worker_pane(
                    project_root=project_root,
                    prompt=task["prompt"],
                    agent=task.get("agent", "claude"),
                    permission_mode=task.get("permission_mode", "acceptEdits"),
                    slug=task["id"],
                    session_root=session_root,
                )
                slugs.append(pane.slug)
                tier_result["tasks"].append(
                    {"id": task["id"], "slug": pane.slug, "status": "created"}
                )
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                tier_result["tasks"].append(
                    {
                        "id": task["id"],
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                failed_ids.add(task["id"])
                results["failed"].append(task["id"])
                new_skips = _transitive_dependents(tasks, failed_ids) - skipped_ids
                skipped_ids.update(new_skips)

        # Wait for all panes in tier
        timeout = max(t.get("timeout", 600) for t in tier) if tier else 600
        start = time.monotonic()
        pending = set(slugs)

        while pending and (time.monotonic() - start < timeout):
            for slug in list(pending):
                rec = _p._get_pane(session_root, slug)
                if _p._is_done(session_root, slug, pane_record=rec):
                    pending.discard(slug)
            if pending:
                time.sleep(3)

        # Merge completed panes
        for slug in slugs:
            if slug in pending:
                tier_result["tasks"] = [
                    {**t, "status": "timed_out"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]
                failed_ids.add(slug)
                results["failed"].append(slug)
                new_skips = _transitive_dependents(tasks, failed_ids) - skipped_ids
                skipped_ids.update(new_skips)
                continue

            merge_result = merge_worker_pane(project_root, slug, session_root=session_root)
            if "merged" in merge_result:
                results["merged"].append(slug)
                tier_result["tasks"] = [
                    {**t, "status": "merged"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]
            else:
                failed_ids.add(slug)
                results["failed"].append(slug)
                new_skips = _transitive_dependents(tasks, failed_ids) - skipped_ids
                skipped_ids.update(new_skips)
                tier_result["tasks"] = [
                    {**t, "status": "merge_failed"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]

        results["tiers"].append(tier_result)

    results["skipped"] = list(skipped_ids)
    return results
