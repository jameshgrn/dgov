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


async def run_headless_worker(
    project_root: str,
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
    config_json = json.dumps({
        "language": pc.language,
        "src_dir": pc.src_dir,
        "test_dir": pc.test_dir,
        "llm_base_url": pc.llm_base_url,
        "llm_api_key_env": pc.llm_api_key_env,
        "test_cmd": pc.test_cmd,
        "lint_cmd": pc.lint_cmd,
        "format_cmd": pc.format_cmd,
        "lint_fix_cmd": pc.lint_fix_cmd,
        "type_check_cmd": pc.type_check_cmd,
        "test_markers": list(pc.test_markers),
        "worker_iteration_budget": pc.worker_iteration_budget,
        "worker_iteration_warn_at": pc.worker_iteration_warn_at,
        "worker_tree_max_lines": pc.worker_tree_max_lines,
        "conventions": dict(pc.conventions) if pc.conventions else None,
        "tool_policy": pc.tool_policy.as_jsonable(),
    })

    cmd = [
        sys.executable,
        "-u",
        str(_WORKER_SCRIPT),
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
