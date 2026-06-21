"""Headless Worker implementation using async subprocesses.

Pillar #2: Atomic Attempt - Runs in an isolated subprocess.
Pillar #6: Event-Sourced - Streams JSON activity to the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dgov.event_types import WorkerLog
from dgov.persistence import emit_event

if TYPE_CHECKING:
    from dgov.dag_parser import DagTaskSpec

logger = logging.getLogger(__name__)

# Absolute path to worker.py — resolved at import time, not runtime.
_WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "worker.py"
_RESEARCHER_SCRIPT = Path(__file__).resolve().parent.parent / "researcher.py"
_PLANNER_SCRIPT = Path(__file__).resolve().parent.parent / "planner.py"
_WORKER_TERMINATE_GRACE_S = 3.0
_WORKER_KILL_GRACE_S = 3.0


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

    payload = load_project_config(project_root).to_worker_payload(task.provider)
    if task.iteration_budget is not None:
        payload["worker_iteration_budget"] = task.iteration_budget
    return json.dumps(payload)


def _worker_command(
    *,
    project_root: str,
    worktree_path: Path,
    task: DagTaskSpec,
    task_scope: Mapping[str, object],
) -> list[str]:
    return [
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
        _config_json_for_task(project_root, task),
        "--task-scope",
        json.dumps(task_scope),
    ]


def _worker_tokens(data: dict[str, object]) -> tuple[int, int] | None:
    token_data = data.get("worker_tokens")
    if not isinstance(token_data, dict):
        return None
    token_data = cast(dict[str, object], token_data)
    return (
        _token_int(token_data.get("prompt_tokens")),
        _token_int(token_data.get("completion_tokens")),
    )


def _token_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _worker_log_event(data: dict[str, object]) -> dict[str, object] | None:
    worker_event = data.get("worker_event")
    return cast(dict[str, object], worker_event) if isinstance(worker_event, dict) else None


def _emit_worker_log(
    *,
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    log_type: str,
    content: object,
) -> None:
    emit_event(
        project_root,
        WorkerLog(
            pane=pane_slug,
            plan_name=plan_name,
            task_slug=task_slug,
            log_type=log_type,
            content=content,
        ),
    )


def _handle_worker_event(
    data: dict[str, object],
    *,
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    on_event: Callable[[str, str, object], None] | None,
) -> str | None:
    ev = _worker_log_event(data)
    if ev is None:
        return None
    raw_type = ev.get("type")
    if not isinstance(raw_type, str) or raw_type == "":
        logger.warning("Worker [%s] emitted malformed worker_event: %r", task_slug, ev)
        return None
    log_type = raw_type
    content = ev.get("content")
    _emit_worker_log(
        project_root=project_root,
        plan_name=plan_name,
        task_slug=task_slug,
        pane_slug=pane_slug,
        log_type=log_type,
        content=content,
    )
    if on_event is not None:
        on_event(task_slug, log_type, content)
    if log_type == "error":
        return str(content) if content else ""
    return None


def _resolved_tool_bin_dirs(names: tuple[str, ...]) -> list[str]:
    """Resolve required tool locations before sanitizing the worker env."""
    dirs: list[str] = []
    seen: set[str] = set()
    for name in names:
        resolved = shutil.which(name)
        if resolved is None:
            continue
        parent = str(Path(resolved).resolve().parent)
        if parent in seen:
            continue
        seen.add(parent)
        dirs.append(parent)
    return dirs


def _build_worker_env(project_root: str, task: DagTaskSpec) -> dict[str, str]:
    """Build a minimal, explicit environment for the worker subprocess."""
    from dgov.config import load_project_config

    config = load_project_config(project_root)
    _, api_key_env = config.llm_runtime_settings(task.provider)
    tool_bin_dirs = _resolved_tool_bin_dirs(("uv", "sg"))

    env: dict[str, str] = {
        "PATH": os.pathsep.join(
            dict.fromkeys((
                str(Path(sys.executable).parent),
                *tool_bin_dirs,
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ))
        )
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "DGOV_RUN_SOURCE", "PYTHONPATH"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    api_key_value = os.environ.get(api_key_env)
    if api_key_value is not None:
        env[api_key_env] = api_key_value

    return env


async def _launch_worker_subprocess(
    cmd: list[str],
    project_root: str,
    env: Mapping[str, str],
) -> asyncio.subprocess.Process:
    """Launch the worker subprocess with stdout piped."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=project_root,
        env=env,
    )
    assert process.stdout is not None
    return process


async def _drain_worker_stdout(
    process: asyncio.subprocess.Process,
    *,
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    on_event: Callable[[str, str, object], None] | None,
) -> tuple[str, int, int]:
    """Drain stdout, parse JSON events, capture tokens and errors.

    Returns (last_error, prompt_tokens, completion_tokens).
    """
    last_error = ""
    prompt_tokens = 0
    completion_tokens = 0

    assert process.stdout is not None

    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        data = _decode_worker_stdout_line(line_bytes, task_slug)
        if data is None:
            continue
        error = _handle_worker_event(
            data,
            project_root=project_root,
            plan_name=plan_name,
            task_slug=task_slug,
            pane_slug=pane_slug,
            on_event=on_event,
        )
        if error is not None:
            last_error = error
        if tokens := _worker_tokens(data):
            prompt_tokens, completion_tokens = tokens

    return last_error, prompt_tokens, completion_tokens


async def _worker_subprocess_result(
    process: asyncio.subprocess.Process,
    *,
    project_root: str,
    plan_name: str,
    task_slug: str,
    pane_slug: str,
    on_event: Callable[[str, str, object], None] | None,
) -> tuple[int, str, int, int]:
    last_error, prompt_tokens, completion_tokens = await _drain_worker_stdout(
        process,
        project_root=project_root,
        plan_name=plan_name,
        task_slug=task_slug,
        pane_slug=pane_slug,
        on_event=on_event,
    )
    return await process.wait(), last_error, prompt_tokens, completion_tokens


def _decode_worker_stdout_line(line_bytes: bytes, task_slug: str) -> dict | None:
    line = line_bytes.decode().strip()
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        if line:
            logger.debug("Worker [%s] raw: %s", task_slug, line)
        return None
    if not isinstance(data, dict):
        logger.warning("Worker [%s] emitted non-object JSON: %r", task_slug, data)
        return None
    return data


def _report_exit(
    on_exit: Callable[[str, str, int, str, int, int], None],
    task_slug: str,
    pane_slug: str,
    exit_code: int,
    last_error: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Report worker exit via the on_exit callback."""
    on_exit(task_slug, pane_slug, exit_code, last_error, prompt_tokens, completion_tokens)


async def _terminate_worker_process(
    process: asyncio.subprocess.Process,
    *,
    task_slug: str,
) -> None:
    if process.returncode is not None:
        return
    logger.warning("Cancelling worker [%s] — terminating subprocess", task_slug)
    try:
        process.terminate()
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("Worker [%s] terminate failed: %s", task_slug, exc)
        return

    if await _wait_for_worker_exit(process, _WORKER_TERMINATE_GRACE_S):
        return

    logger.warning("Worker [%s] did not terminate; killing subprocess", task_slug)
    try:
        process.kill()
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("Worker [%s] kill failed: %s", task_slug, exc)
        return
    await _wait_for_worker_exit(process, _WORKER_KILL_GRACE_S)


async def _wait_for_worker_exit(
    process: asyncio.subprocess.Process,
    timeout_s: float,
) -> bool:
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        return False
    return True


async def _start_worker_for_task(
    *,
    project_root: str,
    worktree_path: Path,
    task: DagTaskSpec,
    task_scope: Mapping[str, object],
) -> asyncio.subprocess.Process:
    cmd = _worker_command(
        project_root=project_root,
        worktree_path=worktree_path,
        task=task,
        task_scope=task_scope,
    )
    env = _build_worker_env(project_root, task)
    return await _launch_worker_subprocess(cmd, project_root, env)


async def _cancel_started_worker(
    process: asyncio.subprocess.Process | None,
    *,
    task_slug: str,
) -> None:
    if process is not None:
        await _terminate_worker_process(process, task_slug=task_slug)


def _report_headless_start_failure(
    on_exit: Callable[[str, str, int, str, int, int], None],
    task_slug: str,
    pane_slug: str,
    exc: Exception,
) -> None:
    logger.error("Headless worker [%s] failed to start: %s", task_slug, exc)
    _report_exit(on_exit, task_slug, pane_slug, 1, str(exc), 0, 0)


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
    process: asyncio.subprocess.Process | None = None

    try:
        process = await _start_worker_for_task(
            project_root=project_root,
            worktree_path=worktree_path,
            task=task,
            task_scope=task_scope,
        )
        exit_code, last_error, prompt_tokens, completion_tokens = await _worker_subprocess_result(
            process,
            project_root=project_root,
            plan_name=plan_name,
            task_slug=task_slug,
            pane_slug=pane_slug,
            on_event=on_event,
        )
        _report_exit(
            on_exit,
            task_slug,
            pane_slug,
            exit_code,
            last_error,
            prompt_tokens,
            completion_tokens,
        )
    except asyncio.CancelledError:
        await _cancel_started_worker(process, task_slug=task_slug)
        raise
    except Exception as exc:
        _report_headless_start_failure(on_exit, task_slug, pane_slug, exc)
