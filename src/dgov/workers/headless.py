"""Headless Worker implementation using async subprocesses.

Pillar #2: Atomic Attempt - Runs in an isolated subprocess.
Pillar #6: Event-Sourced - Streams JSON activity to the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.persistence import emit_event

if TYPE_CHECKING:
    from dgov.dag_parser import DagTaskSpec

logger = logging.getLogger(__name__)

# Absolute path to worker.py — resolved at import time, not runtime.
_WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "worker.py"
_RESEARCHER_SCRIPT = Path(__file__).resolve().parent.parent / "researcher.py"


def _script_for_role(role: str) -> Path:
    """Resolve the worker entrypoint script for a task role."""
    if role == "worker":
        return _WORKER_SCRIPT
    if role == "researcher":
        return _RESEARCHER_SCRIPT
    raise ValueError(f"Unknown task role: {role}")


async def run_headless_worker(
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    worktree_path: Path,
    task: DagTaskSpec,
    on_exit: Callable[[str, str, int, str], None],
    on_event: Callable[[str, str, object], None] | None = None,
) -> None:
    """Execute the headless worker lifecycle."""
    from dgov.config import load_project_config

    # Serialize project config to JSON for the subprocess
    pc = load_project_config(project_root)
    config_json = json.dumps(pc.to_worker_payload())

    cmd = [
        sys.executable,
        "-u",
        str(_script_for_role(task.role)),
        "--goal",
        task.prompt,
        "--worktree",
        str(worktree_path),
        "--model",
        task.agent,
        "--project-config",
        config_json,
    ]

    last_error = ""

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
            except json.JSONDecodeError:
                if line:
                    logger.debug("Worker [%s] raw: %s", task_slug, line)

        exit_code = await process.wait()
        on_exit(task_slug, pane_slug, exit_code, last_error)

    except Exception as exc:
        logger.error("Headless worker [%s] failed to start: %s", task_slug, exc)
        on_exit(task_slug, pane_slug, 1, str(exc))
