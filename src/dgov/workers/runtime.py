"""Shared runtime helpers for worker, planner, and researcher subprocesses."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dgov.workers.atomic import AtomicTools
from dgov.workers.config import (
    AtomicConfig,
    atomic_config_from_payload,
    worker_payload_from_project_toml,
)


@dataclass
class WorkerEvent:
    type: str
    content: Any

    def emit(self) -> None:
        print(json.dumps({"worker_event": self.__dict__}), flush=True)


def load_project_payload(worktree: Path) -> dict[str, object]:
    path = worktree / ".dgov" / "project.toml"
    if not path.exists():
        return worker_payload_from_project_toml({})
    try:
        import tomllib

        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    return worker_payload_from_project_toml(raw)


def load_project_config(worktree: Path) -> AtomicConfig:
    return atomic_config_from_payload(load_project_payload(worktree))


def resolve_config(worktree: Path, project_config_json: str) -> AtomicConfig:
    if project_config_json:
        try:
            return atomic_config_from_payload(json.loads(project_config_json))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid worker project config JSON: {exc.msg}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid worker project config payload: {exc}") from exc
    return load_project_config(worktree)


_PROMPT_CONTEXT_MAX_CHARS = 12_000
_TOOL_RESULT_MAX_CHARS = 12_000
_REPO_MAP_TRUNCATION_NOTICE = "\n... [repo map truncated for prompt budget]"
_ENDGAME_MIN_BUDGET = 10
_ENDGAME_REMAINING_CALLS = 8
_FORCE_DONE_REMAINING_CALLS = 2
_EXHAUSTION_SUMMARY_MAX_CHARS = 2_000
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REPO_MAP_SYMBOL_LIMIT = 8
_REPO_MAP_METHOD_LIMIT = 5
_TOOL_ERROR_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("policy_blocked", ("not allowed",)),
    ("not_found", ("not found", "no such file", "does not exist")),
    ("ambiguous_match", ("multiple", "ambiguous")),
    ("timeout", ("timed out", "timeout")),
    ("command_failed", ("exit code", "command failed")),
    ("validation_failed", ("requires", "invalid", "malformed", "must")),
)


def _is_repo_map_candidate(path: Path, worktree: Path) -> bool:
    if not path.is_file():
        return False
    rel_parts = path.relative_to(worktree).parts
    if any(part.startswith(".") for part in rel_parts):
        return False
    return not any(part in {"__pycache__", "node_modules", ".venv"} for part in rel_parts)


def _repo_map_priority(path: Path, worktree: Path, config: AtomicConfig) -> tuple[int, str]:
    rel = str(path.relative_to(worktree))
    src_root = config.src_dir.rstrip("/")
    test_root = config.test_dir.rstrip("/")
    if src_root and rel.startswith(f"{src_root}/"):
        return (0, rel)
    if test_root and rel.startswith(f"{test_root}/"):
        return (1, rel)
    if path.suffix == ".py":
        return (2, rel)
    return (3, rel)


def _iter_repo_map_files(worktree: Path, config: AtomicConfig) -> list[Path]:
    files = [
        path for path in sorted(worktree.rglob("*")) if _is_repo_map_candidate(path, worktree)
    ]
    return sorted(files, key=lambda path: _repo_map_priority(path, worktree, config))


def _python_symbol_lines(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    return _module_symbol_lines(tree)


def _module_symbol_lines(tree: ast.Module) -> list[str]:
    lines: list[str] = []
    for node in ast.iter_child_nodes(tree):
        lines.extend(_top_level_symbol_lines(node))
        if len(lines) >= _REPO_MAP_SYMBOL_LIMIT:
            break
    return lines[:_REPO_MAP_SYMBOL_LIMIT]


def _top_level_symbol_lines(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ClassDef):
        return [f"class {node.name}", *_class_method_symbol_lines(node)]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return [f"def {node.name}"]
    return []


def _class_method_symbol_lines(node: ast.ClassDef) -> list[str]:
    lines: list[str] = []
    for item in ast.iter_child_nodes(node):
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.append(f"  def {node.name}.{item.name}")
        if len(lines) >= _REPO_MAP_METHOD_LIMIT:
            break
    return lines


def repo_map_snapshot(
    worktree: Path,
    config: AtomicConfig,
    max_lines: int = 80,
    max_chars: int = _PROMPT_CONTEXT_MAX_CHARS,
) -> str:
    lines: list[str] = []
    for path in _iter_repo_map_files(worktree, config):
        rel = str(path.relative_to(worktree))
        lines.append(rel)
        for symbol in _python_symbol_lines(path):
            lines.append(f"  {symbol}")

    if max_lines > 0:
        lines = lines[:max_lines]

    repo_map = "\n".join(lines)
    if max_chars <= 0 or len(repo_map) <= max_chars:
        return repo_map

    budget = max_chars - len(_REPO_MAP_TRUNCATION_NOTICE)
    if budget <= 0:
        return _REPO_MAP_TRUNCATION_NOTICE.lstrip("\n")

    kept: list[str] = []
    used = 0
    for line in lines:
        line_len = len(line) + (1 if kept else 0)
        if used + line_len > budget:
            break
        kept.append(line)
        used += line_len
    return "\n".join(kept) + _REPO_MAP_TRUNCATION_NOTICE


def clip_tool_result(result: str, max_chars: int = _TOOL_RESULT_MAX_CHARS) -> str:
    if max_chars <= 0 or len(result) <= max_chars:
        return result
    notice = "\n... [tool output truncated for prompt budget]"
    budget = max_chars - len(notice)
    if budget <= 0:
        return notice.lstrip("\n")
    return result[:budget] + notice


def _clip_tool_result_with_stats(
    result: str,
    max_chars: int = _TOOL_RESULT_MAX_CHARS,
) -> tuple[str, dict[str, int | bool]]:
    clipped = clip_tool_result(result, max_chars=max_chars)
    return clipped, {
        "result_chars": len(clipped),
        "raw_result_chars": len(result),
        "result_clipped": clipped != result,
    }


def _classify_tool_error(result: str) -> str:
    text = result.lower()
    for kind, patterns in _TOOL_ERROR_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return kind
    return "unknown"


def _tool_call_id(call: Any) -> str:
    call_id = getattr(call, "id", "")
    return call_id if isinstance(call_id, str) else ""


def _duration_ms(start: float) -> float:
    return round(max(0.0, time.perf_counter() - start) * 1000, 3)


def iteration_budget(config: AtomicConfig) -> int:
    budget = config.worker_iteration_budget
    return budget if budget > 0 else 1


def _remaining_iterations(iteration: int, budget: int) -> int:
    return max(0, budget - iteration)


def should_enter_endgame(iteration: int, budget: int) -> bool:
    return (
        budget >= _ENDGAME_MIN_BUDGET
        and _remaining_iterations(iteration, budget) <= _ENDGAME_REMAINING_CALLS
    )


def should_force_done(iteration: int, budget: int) -> bool:
    return (
        budget >= _ENDGAME_MIN_BUDGET
        and _remaining_iterations(iteration, budget) <= _FORCE_DONE_REMAINING_CALLS
    )


def tool_choice_for_iteration(iteration: int, budget: int) -> str | dict[str, object]:
    if should_force_done(iteration, budget):
        return {"type": "function", "function": {"name": "done"}}
    return "auto"


def endgame_prompt(iteration: int, budget: int) -> str:
    remaining = _remaining_iterations(iteration, budget)
    return (
        f"FINALIZATION MODE: {remaining}/{budget} tool calls remain before the hard "
        "iteration limit. Stop broad implementation and stop exploring. Use the "
        "remaining calls only to review the current diff, run the narrowest relevant "
        "verification that has not already run, make tiny fixes for direct syntax, "
        "lint, or test failures, and call `done`. If the work is incomplete or "
        "blocked, call `done` with an INCOMPLETE summary that names the changed "
        "files, verification status, and blocker. The Governor validates after "
        "`done`; exhausting the budget is worse than an explicit incomplete handoff."
    )


def force_done_prompt() -> str:
    return (
        "HARD STOP: call the `done` tool now. Summarize changed files, verification "
        "commands and results, and any incomplete work or blocker. Do not call any "
        "other tool."
    )


def diff_stat_for_error(worktree: Path) -> str:
    try:
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        summary = diff.stdout.strip()
        if not summary:
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            summary = status.stdout.strip() or "No changes."
        if len(summary) <= _EXHAUSTION_SUMMARY_MAX_CHARS:
            return summary
        return summary[:_EXHAUSTION_SUMMARY_MAX_CHARS] + "\n... [diff summary truncated]"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Unavailable: {exc}"


def task_scope_section(task_scope: Mapping[str, object] | None) -> str:
    if not task_scope:
        return ""

    def _paths(name: str) -> list[str]:
        raw = task_scope.get(name, [])
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    task_slug = str(task_scope.get("task_slug", "")).strip()
    writable = list(
        dict.fromkeys([
            *_paths("create"),
            *_paths("edit"),
            *_paths("delete"),
            *_paths("touch"),
        ])
    )
    read_only = _paths("read")
    lines = ["\nTASK SCOPE:"]
    if task_slug:
        lines.append(f"- Task: {task_slug}")
    lines.append(
        f"- Writable paths: {', '.join(writable) if writable else '(none; read-only task)'}"
    )
    if read_only:
        lines.append(f"- Read-only context: {', '.join(read_only)}")
    verify_test_targets = _paths("verify_test_targets")
    if verify_test_targets:
        lines.append(f"- Verification test targets: {', '.join(verify_test_targets)}")
    if task_scope.get("require_successful_test_verification") is True:
        lines.append("- Retry completion gate: run_tests() must pass before done.")
        command = str(task_scope.get("required_verification_command", "")).strip()
        if command:
            lines.append(f"- Settlement failing command: {command}")
    lines.extend([
        "- Every other path is out of scope, even if it looks related.",
        "- If a path claimed under files.create already exists in this worktree, treat it as"
        " an in-scope existing file and edit it in place rather than widening scope.",
        "- Before done, run scope_status to preview modified and transient file scope.",
        "- Before finishing, verify that unclaimed files stayed unchanged.",
    ])
    return "\n".join(lines)


def _validate_plan(args: dict[str, Any]) -> str | None:
    tasks, error = _extract_plan_tasks(args)
    if error is not None:
        return error
    return (
        _validate_plan_task_contracts(tasks)
        or _validate_plan_dependencies(tasks)
        or _validate_plan_cycles(tasks)
    )


def _extract_plan_tasks(args: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    raw_tasks = args.get("tasks")
    if not raw_tasks or not isinstance(raw_tasks, list):
        return [], "Error: emit_plan requires at least one task."

    tasks: list[dict[str, Any]] = []
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            return [], f"Error: Task {index} is not a dict."
        tasks.append(cast(dict[str, Any], raw_task))
    return tasks, None


def _validate_plan_task_contracts(tasks: list[dict[str, Any]]) -> str | None:
    slugs: set[str] = set()
    for index, task in enumerate(tasks):
        error = _validate_plan_task_contract(index, task, slugs)
        if error is not None:
            return error
    return None


def _validate_plan_task_contract(
    index: int,
    task: dict[str, Any],
    slugs: set[str],
) -> str | None:
    slug = _task_slug(task)
    if not slug or not _SLUG_RE.match(slug):
        return f"Error: Task {index} has invalid slug {slug!r}. Must match [A-Za-z0-9_-]+."
    if slug in slugs:
        return f"Error: Duplicate task slug {slug!r}."
    slugs.add(slug)

    for field_name in ("prompt", "commit_message"):
        if not _task_text(task, field_name):
            return f"Error: Task {slug!r} has empty {field_name}."

    if task.get("role", "worker") == "worker" and not _worker_task_claims_file(task):
        return (
            f"Error: Worker task {slug!r} must claim at least one file (create, edit, or touch)."
        )
    return None


def _task_slug(task: dict[str, Any]) -> str:
    slug = task.get("slug", "")
    return slug if isinstance(slug, str) else ""


def _task_text(task: dict[str, Any], key: str) -> str:
    value = task.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _worker_task_claims_file(task: dict[str, Any]) -> bool:
    files = task.get("files", {})
    if not isinstance(files, dict):
        return False
    return any(_file_claim_has_values(files, claim) for claim in ("create", "edit", "touch"))


def _file_claim_has_values(files: dict[Any, Any], claim: str) -> bool:
    value = files.get(claim)
    return isinstance(value, list) and bool(value)


def _validate_plan_dependencies(tasks: list[dict[str, Any]]) -> str | None:
    slug_set = {_task_slug(task) for task in tasks}
    for task in tasks:
        slug = _task_slug(task)
        dependencies = _task_dependencies(task)
        if dependencies is None:
            return f"Error: Task {slug!r} has invalid depends_on. Must be a list."
        for dep in dependencies:
            if dep not in slug_set:
                return f"Error: Task {slug!r} depends on unknown slug {dep!r}."
    return None


def _task_dependencies(task: dict[str, Any]) -> list[str] | None:
    raw_dependencies = task.get("depends_on", [])
    if not isinstance(raw_dependencies, list):
        return None
    if not all(isinstance(dep, str) for dep in raw_dependencies):
        return None
    return cast(list[str], raw_dependencies)


def _validate_plan_cycles(tasks: list[dict[str, Any]]) -> str | None:
    dep_map = {_task_slug(task): _task_dependencies(task) or [] for task in tasks}
    visited: set[str] = set()
    in_stack: set[str] = set()
    for slug in dep_map:
        if _plan_has_cycle(slug, dep_map, visited, in_stack):
            return f"Error: Dependency cycle detected involving {slug!r}."
    return None


def _plan_has_cycle(
    slug: str,
    dep_map: dict[str, list[str]],
    visited: set[str],
    in_stack: set[str],
) -> bool:
    if slug in in_stack:
        return True
    if slug in visited:
        return False
    visited.add(slug)
    in_stack.add(slug)
    for dep in dep_map.get(slug, []):
        if _plan_has_cycle(dep, dep_map, visited, in_stack):
            return True
    in_stack.discard(slug)
    return False


def _tool_base_event(
    name: str,
    args: dict[str, Any],
    call_id: str | None,
    role: str,
    turn_index: int,
    tool_index: int,
) -> dict[str, Any]:
    return {
        "tool": name,
        "args": args,
        "arg_keys": sorted(args),
        "call_id": call_id,
        "role": role,
        "turn_index": turn_index,
        "tool_index": tool_index,
    }


def _emit_tool_result(
    base_event: Mapping[str, Any],
    start: float,
    result: str,
    status: str,
    activity: list[dict[str, Any]],
) -> str:
    clipped_result, result_stats = _clip_tool_result_with_stats(result)
    content: dict[str, Any] = {
        **base_event,
        "status": status,
        "activity": activity,
        "duration_ms": _duration_ms(start),
        **result_stats,
    }
    if status == "failed":
        content["error_kind"] = _classify_tool_error(result)
    WorkerEvent("result", content).emit()
    return clipped_result


def _execute_done_tool(
    args: dict[str, Any],
    actuators: AtomicTools,
    base_event: Mapping[str, Any],
    start: float,
) -> tuple[str, bool]:
    verification_error = actuators._done_verification_error()
    if verification_error is not None:
        return _emit_tool_result(base_event, start, verification_error, "failed", []), False
    summary = args.get("summary", "")
    _emit_tool_result(base_event, start, summary, "success", [])
    WorkerEvent("done", args.get("summary")).emit()
    return args.get("summary", ""), True


def _execute_emit_plan_tool(
    args: dict[str, Any],
    base_event: Mapping[str, Any],
    start: float,
) -> tuple[str, bool]:
    error = _validate_plan(args)
    if error:
        return _emit_tool_result(base_event, start, error, "failed", []), False
    result = args.get("summary", "Plan emitted.")
    _emit_tool_result(base_event, start, result, "success", [])
    WorkerEvent("plan", args).emit()
    return args.get("summary", "Plan emitted."), True


def _execute_ask_user_tool(
    args: dict[str, Any],
    ask_user_fn: Callable[[str], str] | None,
    base_event: Mapping[str, Any],
    start: float,
) -> tuple[str, bool]:
    if ask_user_fn is None:
        result = "Error: ask_user is not available in autonomous mode."
        return _emit_tool_result(base_event, start, result, "failed", []), False
    answer = ask_user_fn(args.get("question", ""))
    return _emit_tool_result(base_event, start, answer, "success", []), False


def _execute_actuator_tool(
    name: str,
    args: dict[str, Any],
    actuators: AtomicTools,
    base_event: Mapping[str, Any],
    start: float,
) -> tuple[str, bool]:
    func = getattr(actuators, name, None)
    result = func(**args) if func else f"Error: Unknown tool {name}"
    activity = actuators._consume_activity()
    status = "failed" if result.startswith("Error:") else "success"
    return _emit_tool_result(base_event, start, result, status, activity), False


def execute_tool_call(
    call: Any,
    actuators: AtomicTools,
    allowed_tools: frozenset[str] | None = None,
    ask_user_fn: Callable[[str], str] | None = None,
    *,
    role: str = "worker",
    turn_index: int = 0,
    tool_index: int = 0,
) -> tuple[str, bool]:
    name = call.function.name
    call_id = _tool_call_id(call)
    start = time.perf_counter()
    try:
        args = json.loads(call.function.arguments)
    except json.JSONDecodeError as exc:
        args = {}
        base_event = _tool_base_event(name, args, call_id, role, turn_index, tool_index)
        WorkerEvent("call", base_event).emit()
        result = f"Error: Tool {name} arguments contain invalid JSON: {exc.msg}"
        return _emit_tool_result(base_event, start, result, "failed", []), False
    if not isinstance(args, dict):
        args = {}
        base_event = _tool_base_event(name, args, call_id, role, turn_index, tool_index)
        WorkerEvent("call", base_event).emit()
        result = f"Error: Tool {name} arguments must be a JSON object."
        return _emit_tool_result(base_event, start, result, "failed", []), False
    base_event = _tool_base_event(name, args, call_id, role, turn_index, tool_index)
    WorkerEvent("call", base_event).emit()

    if allowed_tools is not None and name not in allowed_tools:
        result = f"Error: Tool {name} is not allowed in this worker role."
        return _emit_tool_result(base_event, start, result, "failed", []), False

    if name == "done":
        return _execute_done_tool(args, actuators, base_event, start)

    if name == "emit_plan":
        return _execute_emit_plan_tool(args, base_event, start)

    if name == "ask_user":
        return _execute_ask_user_tool(args, ask_user_fn, base_event, start)

    return _execute_actuator_tool(name, args, actuators, base_event, start)
