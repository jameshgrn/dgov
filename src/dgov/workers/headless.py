"""Headless Worker implementation using async subprocesses.

Pillar #2: Atomic Attempt - Runs in an isolated subprocess.
Pillar #6: Event-Sourced - Streams JSON activity to the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.persistence import emit_event

if TYPE_CHECKING:
    from dgov.dag_parser import DagTaskSpec

logger = logging.getLogger(__name__)


async def run_headless_worker(
    project_root: str,
    task_slug: str,
    pane_slug: str,
    worktree_path: Path,
    task: DagTaskSpec,
    pipe_path: Path,
    loop: asyncio.AbstractEventLoop,
    on_exit: callable,
) -> None:
    """Execute the headless worker lifecycle."""

    # Pillar #10: Use current executable to bypass 'uv run' overhead
    cmd = [
        sys.executable,
        "src/dgov/worker.py",
        "--goal",
        task.prompt,
        "--worktree",
        str(worktree_path),
        "--model",
        task.agent,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_root,
        )

        # Pillar #6: Async event stream processing
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode().strip()
            try:
                data = json.loads(line)
                if "worker_event" in data:
                    ev = data["worker_event"]
                    # Map worker events to journal (Pillar #6 condensation)
                    emit_event(
                        project_root,
                        "worker_log",
                        pane_slug,
                        task_slug=task_slug,
                        log_type=ev["type"],
                        content=ev.get("content"),
                    )
            except json.JSONDecodeError:
                if line:
                    logger.debug("Worker [%s] raw: %s", task_slug, line)

        exit_code = await process.wait()

        # Pillar #10: Unify signaling via the provided callback
        on_exit(task_slug, pane_slug, exit_code)

    except Exception as exc:
        logger.error("Headless worker [%s] failed to start: %s", task_slug, exc)
        on_exit(task_slug, pane_slug, 1)
