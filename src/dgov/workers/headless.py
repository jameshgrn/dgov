"""Headless Worker implementation using async subprocesses.

Pillar #2: Atomic Attempt - Runs in an isolated subprocess.
Pillar #6: Event-Sourced - Streams JSON activity to the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.persistence import emit_event

if TYPE_CHECKING:
    from dgov.dag_parser import DagTaskSpec

logger = logging.getLogger(__name__)

# Absolute path to worker.py — resolved at import time, not runtime.
_WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "worker.py"
_RESEARCHER_SCRIPT = Path(__file__).resolve().parent.parent / "researcher.py"
_PLANNER_SCRIPT = Path(__file__).resolve().parent.parent / "planner.py"


def _script_for_role(role: str) -> Path:
    """Resolve the worker entrypoint script for a task role."""
    if role == "worker":
        return _WORKER_SCRIPT
    if role in ("researcher", "reviewer"):
        return _RESEARCHER_SCRIPT
    if role == "planner":
        return _PLANNER_SCRIPT
    raise ValueError(f"Unknown task role: {role}")


def _config_json_for_task(project_root: str, task: DagTaskSpec) -> str:
    """Serialize worker config, applying task-local overrides in memory only."""
    from dgov.config import load_project_config

    payload = load_project_config(project_root).to_worker_payload()
    if task.iteration_budget is not None:
        payload["worker_iteration_budget"] = task.iteration_budget
    return json.dumps(payload)


async def run_headless_worker(
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    worktree_path: Path,
    task: DagTaskSpec,
    task_scope: Mapping[str, object],
    on_exit: Callable[[str, str, int, str, int, int], None],
    on_event: Callable[[str, str, object], None] | None = None,
) -> None:
    """Execute the headless worker lifecycle."""
    config_json = _config_json_for_task(project_root, task)

    cmd = [
        sys.executable,
        "-u",
        str(_script_for_role(task.role)),
        "--goal",
        task.prompt or "",
        "--worktree",
        str(worktree_path),
        "--model",
        task.agent or "",
        "--project-config",
        config_json,
        "--task-scope",
        json.dumps(task_scope),
    ]

    last_error = ""
    prompt_tokens = 0
    completion_tokens = 0

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_root,
        )

        assert process.stdout is not None

        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode().strip()
            try:
                data = json.loads(line)
                if "worker_event" in data:
                    ev = data["worker_event"]
                    log_type = ev["type"]
                    content = ev.get("content")
                    emit_event(
                        project_root,
                        "worker_log",
                        pane_slug,
                        plan_name=plan_name,
                        task_slug=task_slug,
                        log_type=log_type,
                        content=content,
                    )
                    if log_type == "error":
                        last_error = str(content) if content else ""
                    if on_event is not None:
                        on_event(task_slug, log_type, content)
                elif "worker_tokens" in data:
                    token_data = data["worker_tokens"]
                    prompt_tokens = token_data.get("prompt_tokens", 0)
                    completion_tokens = token_data.get("completion_tokens", 0)
            except json.JSONDecodeError:
                if line:
                    logger.debug("Worker [%s] raw: %s", task_slug, line)

        exit_code = await process.wait()
        on_exit(task_slug, pane_slug, exit_code, last_error, prompt_tokens, completion_tokens)

    except Exception as exc:
        logger.error("Headless worker [%s] failed to start: %s", task_slug, exc)
        on_exit(task_slug, pane_slug, 1, str(exc), 0, 0)
