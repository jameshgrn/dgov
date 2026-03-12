# Batch execution with DAG-ordered parallelism.
"""DAG-ordered batch execution for workstation tasks.

Groups tasks into tiers based on file-touch conflicts. Tasks within a tier
run in parallel (disjoint files). Tiers execute sequentially so merges
don't conflict.

Uses distributary's DAG engine for tier computation and TmuxWorkerBackend
for worker lifecycle (spawn/wait/cleanup via tmux panes).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dgov._dag_compat import get_tiers
from dgov.models import TaskSpec


@dataclass
class BatchTask:
    id: str
    prompt: str
    agent: str = "claude"
    touches: list[str] = field(default_factory=list)
    permission_mode: str = "acceptEdits"
    timeout: int = 600


def _to_task_specs(tasks: list[BatchTask]) -> dict[str, TaskSpec]:
    """Convert BatchTasks to distributary TaskSpecs for DAG computation."""
    return {
        t.id: TaskSpec(
            id=t.id,
            description=t.prompt,
            exports=[],
            imports=[],
            touches=t.touches,
            body=t.prompt,
            timeout=t.timeout,
            worker_cmd=t.agent if t.agent != "claude" else None,
            permission_mode=t.permission_mode,
        )
        for t in tasks
    }


def compute_tiers(tasks: list[BatchTask]) -> list[list[str]]:
    """Build dependency graph from touch conflicts, return execution tiers.

    Delegates to distributary's DAG engine which handles import/export deps,
    touch overlap serialization, and topological sorting.
    """
    if not tasks:
        return []
    return get_tiers(_to_task_specs(tasks))


def load_batch(path: str) -> tuple[str, list[BatchTask]]:
    """Load a batch spec from a JSON file.

    Returns (project_root, tasks).
    """
    with open(path) as f:
        data = json.load(f)

    project_root = data.get("project_root", ".")
    tasks = []
    for t in data["tasks"]:
        tasks.append(
            BatchTask(
                id=t["id"],
                prompt=t["prompt"],
                agent=t.get("agent", "pi"),
                touches=t.get("touches", []),
                permission_mode=t.get("permission_mode", "acceptEdits"),
                timeout=t.get("timeout", 600),
            )
        )
    return project_root, tasks


async def _run_batch_async(
    project_root: str,
    tasks: list[BatchTask],
    session_root: str,
) -> dict:
    """Async batch execution using TmuxWorkerBackend for spawn/wait/cleanup."""
    from dgov.panes import close_worker_pane, merge_worker_pane
    from dgov.worker_backend import TmuxWorkerBackend

    tiers = compute_tiers(tasks)
    task_map = {t.id: t for t in tasks}
    specs = _to_task_specs(tasks)
    backend = TmuxWorkerBackend(session_root=session_root)

    results: dict = {
        "tiers": [],
        "total_tasks": len(tasks),
        "merged": [],
        "failed": [],
    }

    for tier_idx, tier_ids in enumerate(tiers):
        tier_result: dict = {"tier": tier_idx, "tasks": {}}

        # Spawn all tasks in this tier concurrently
        handles: dict[str, object] = {}
        env = dict(os.environ)
        for task_id in tier_ids:
            spec = specs[task_id]
            wt_path = Path(project_root) / ".workstation" / "worktrees" / task_id
            handle = await backend.spawn(spec, wt_path, env)
            handles[task_id] = handle
            tier_result["tasks"][task_id] = {"status": "launched"}

        # Wait for all tasks concurrently
        async def _wait_task(tid: str) -> bool:
            return await backend.wait(handles[tid], task_map[tid].timeout)

        wait_results = await asyncio.gather(*[_wait_task(tid) for tid in tier_ids])

        for task_id, succeeded in zip(tier_ids, wait_results):
            tier_result["tasks"][task_id]["status"] = "done" if succeeded else "timeout"

        # Merge completed tasks, clean up all
        tier_ok = True
        for task_id in tier_ids:
            status = tier_result["tasks"][task_id]["status"]
            if status == "timeout":
                await backend.cleanup(handles[task_id])
                results["failed"].append(task_id)
                tier_ok = False
                continue

            merge_result = merge_worker_pane(
                project_root, task_id, session_root=session_root, resolve="agent"
            )
            if merge_result.get("merged"):
                tier_result["tasks"][task_id]["merge"] = "ok"
                results["merged"].append(task_id)
            else:
                tier_result["tasks"][task_id]["merge"] = "failed"
                tier_result["tasks"][task_id]["merge_detail"] = merge_result
                results["failed"].append(task_id)
                tier_ok = False

        # Close all panes
        for task_id in tier_ids:
            close_worker_pane(project_root, task_id, session_root=session_root)

        results["tiers"].append(tier_result)

        if not tier_ok:
            remaining = [tid for tier in tiers[tier_idx + 1 :] for tid in tier]
            if remaining:
                results["aborted_remaining"] = remaining
            break

    return results


def run_batch(
    project_root: str,
    tasks: list[BatchTask],
    session_root: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a batch of tasks in DAG-ordered tiers.

    Uses TmuxWorkerBackend for spawn/wait/cleanup and distributary's
    DAG engine for tier computation.

    Returns a summary dict with tier results.
    """
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root

    if dry_run:
        tiers = compute_tiers(tasks)
        return {
            "dry_run": True,
            "tiers": [list(tier) for tier in tiers],
            "total_tasks": len(tasks),
            "total_tiers": len(tiers),
        }

    return asyncio.run(_run_batch_async(project_root, tasks, session_root))
