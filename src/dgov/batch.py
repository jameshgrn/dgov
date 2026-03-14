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
# Batch execution
# ---------------------------------------------------------------------------


def _compute_tiers(tasks: list[dict]) -> list[list[dict]]:
    """Group tasks into parallel tiers based on file touches.

    Tasks with disjoint `touches` go into the same tier.
    Tasks with overlapping touches are serialized into subsequent tiers.
    """
    tiers: list[list[dict]] = []
    remaining = list(tasks)

    while remaining:
        tier: list[dict] = []
        tier_touches: set[str] = set()
        next_remaining: list[dict] = []

        for task in remaining:
            task_touches = set(task.get("touches", []))
            has_overlap = False
            for tt in task_touches:
                for et in tier_touches:
                    if tt == et or tt.startswith(et) or et.startswith(tt):
                        has_overlap = True
                        break
                if has_overlap:
                    break

            if not has_overlap:
                tier.append(task)
                tier_touches.update(task_touches)
            else:
                next_remaining.append(task)

        if tier:
            tiers.append(tier)
        remaining = next_remaining

    return tiers


def run_batch(
    spec_path: str,
    session_root: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a batch spec: create panes, wait, merge in tier order.

    Spec format (JSON file):
    {
        "project_root": "/path/to/repo",
        "tasks": [
            {"id": "task-1", "prompt": "...", "agent": "pi", "touches": ["src/foo.py"]},
            {"id": "task-2", "prompt": "...", "agent": "claude", "touches": ["tests/"]}
        ]
    }

    Tasks with disjoint `touches` run in parallel tiers.
    Overlapping touches are serialized.
    """
    import dgov.panes as _p
    from dgov.merger import merge_worker_pane

    with open(spec_path) as f:
        spec = json.load(f)

    project_root = spec["project_root"]
    tasks = spec["tasks"]
    session_root = os.path.abspath(session_root or project_root)

    tiers = _compute_tiers(tasks)

    if dry_run:
        return {
            "dry_run": True,
            "tiers": [[t["id"] for t in tier] for tier in tiers],
            "total_tasks": len(tasks),
        }

    results: dict = {"tiers": [], "merged": [], "failed": []}

    for tier_idx, tier in enumerate(tiers):
        tier_result: dict = {"tier": tier_idx, "tasks": []}

        # Create all panes in this tier
        slugs = []
        for task in tier:
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
                    {"id": task["id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                )
                results["failed"].append(task["id"])

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
                results["failed"].append(slug)
                continue

            merge_result = merge_worker_pane(project_root, slug, session_root=session_root)
            if "merged" in merge_result:
                results["merged"].append(slug)
                tier_result["tasks"] = [
                    {**t, "status": "merged"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]
            else:
                results["failed"].append(slug)
                tier_result["tasks"] = [
                    {**t, "status": "merge_failed"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]

        results["tiers"].append(tier_result)

        # Abort remaining tiers if any failure
        if results["failed"]:
            results["aborted_remaining"] = True
            break

    return results
