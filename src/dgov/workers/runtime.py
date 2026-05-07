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
    except Exception:
        return worker_payload_from_project_toml({})
    return worker_payload_from_project_toml(raw)


def load_project_config(worktree: Path) -> AtomicConfig:
    return atomic_config_from_payload(load_project_payload(worktree))


def resolve_config(worktree: Path, project_config_json: str) -> AtomicConfig:
    if project_config_json:
        try:
            return atomic_config_from_payload(json.loads(project_config_json))
        except Exception:
            pass
    return load_project_config(worktree)


_PROMPT_CONTEXT_MAX_CHARS = 12_000
_TOOL_RESULT_MAX_CHARS = 12_000
_REPO_MAP_TRUNCATION_NOTICE = "\n... [repo map truncated for prompt budget]"
_ENDGAME_MIN_BUDGET = 10
_ENDGAME_REMAINING_CALLS = 8
_FORCE_DONE_REMAINING_CALLS = 2
_EXHAUSTION_SUMMARY_MAX_CHARS = 2_000
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _iter_repo_map_files(worktree: Path, config: AtomicConfig) -> list[Path]:
    files: list[Path] = []
    for path in sorted(worktree.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(worktree).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in {"__pycache__", "node_modules", ".venv"} for part in rel_parts):
            continue
        files.append(path)

    def _priority(path: Path) -> tuple[int, str]:
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

    return sorted(files, key=_priority)


def _python_symbol_lines(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []

    lines: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}")
            method_count = 0
            for item in ast.iter_child_nodes(node):
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    lines.append(f"  def {node.name}.{item.name}")
                    method_count += 1
                    if method_count >= 5:
                        break
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.append(f"def {node.name}")
        if len(lines) >= 8:
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
    if "not allowed" in text:
        return "policy_blocked"
    if "not found" in text or "no such file" in text or "does not exist" in text:
        return "not_found"
    if "multiple" in text or "ambiguous" in text:
        return "ambiguous_match"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "exit code" in text or "command failed" in text:
        return "command_failed"
    if "requires" in text or "invalid" in text or "malformed" in text or "must" in text:
        return "validation_failed"
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
        "- Before finishing, verify that unclaimed files stayed unchanged.",
    ])
    return "\n".join(lines)


def _validate_plan(args: dict[str, Any]) -> str | None:
    tasks = args.get("tasks")
    if not tasks or not isinstance(tasks, list):
        return "Error: emit_plan requires at least one task."

    slugs: list[str] = []
    for i, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            return f"Error: Task {i} is not a dict."
        task = cast(dict[str, Any], raw_task)
        slug = task.get("slug", "")
        if not slug or not _SLUG_RE.match(slug):
            return f"Error: Task {i} has invalid slug {slug!r}. Must match [A-Za-z0-9_-]+."
        if slug in slugs:
            return f"Error: Duplicate task slug {slug!r}."
        slugs.append(slug)

        if not task.get("prompt", "").strip():
            return f"Error: Task {slug!r} has empty prompt."
        if not task.get("commit_message", "").strip():
            return f"Error: Task {slug!r} has empty commit_message."

        role = task.get("role", "worker")
        if role == "worker":
            files = task.get("files", {})
            has_files = any(
                files.get(k) for k in ("create", "edit", "touch") if isinstance(files.get(k), list)
            )
            if not has_files:
                return (
                    f"Error: Worker task {slug!r} must claim at least one file "
                    "(create, edit, or touch)."
                )

    slug_set = set(slugs)
    for raw in tasks:
        t = cast(dict[str, Any], raw)
        for dep in t.get("depends_on", []):
            if dep not in slug_set:
                return f"Error: Task {t['slug']!r} depends on unknown slug {dep!r}."

    visited: set[str] = set()
    in_stack: set[str] = set()
    dep_map = {t["slug"]: t.get("depends_on", []) for t in tasks}

    def _has_cycle(slug: str) -> bool:
        if slug in in_stack:
            return True
        if slug in visited:
            return False
        visited.add(slug)
        in_stack.add(slug)
        for dep in dep_map.get(slug, []):
            if _has_cycle(dep):
                return True
        in_stack.discard(slug)
        return False

    for slug in slugs:
        if _has_cycle(slug):
            return f"Error: Dependency cycle detected involving {slug!r}."

    return None


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
    args = json.loads(call.function.arguments)
    call_id = _tool_call_id(call)
    start = time.perf_counter()
    base_event = {
        "tool": name,
        "args": args,
        "arg_keys": sorted(args),
        "call_id": call_id,
        "role": role,
        "turn_index": turn_index,
        "tool_index": tool_index,
    }
    WorkerEvent("call", base_event).emit()

    def _emit_result(result: str, status: str, activity: list[dict[str, Any]]) -> str:
        clipped_result, result_stats = _clip_tool_result_with_stats(result)
        content: dict[str, Any] = {
            "tool": name,
            "status": status,
            "activity": activity,
            "call_id": call_id,
            "role": role,
            "turn_index": turn_index,
            "tool_index": tool_index,
            "duration_ms": _duration_ms(start),
            **result_stats,
        }
        if status == "failed":
            content["error_kind"] = _classify_tool_error(result)
        WorkerEvent("result", content).emit()
        return clipped_result

    if allowed_tools is not None and name not in allowed_tools:
        result = f"Error: Tool {name} is not allowed in this worker role."
        return _emit_result(result, "failed", []), False

    if name == "done":
        verification_error = actuators._done_verification_error()
        if verification_error is not None:
            return _emit_result(verification_error, "failed", []), False
        summary = args.get("summary", "")
        _emit_result(summary, "success", [])
        WorkerEvent("done", args.get("summary")).emit()
        return args.get("summary", ""), True

    if name == "emit_plan":
        error = _validate_plan(args)
        if error:
            return _emit_result(error, "failed", []), False
        result = args.get("summary", "Plan emitted.")
        _emit_result(result, "success", [])
        WorkerEvent("plan", args).emit()
        return args.get("summary", "Plan emitted."), True

    if name == "ask_user":
        if ask_user_fn is None:
            result = "Error: ask_user is not available in autonomous mode."
            return _emit_result(result, "failed", []), False
        answer = ask_user_fn(args.get("question", ""))
        return _emit_result(answer, "success", []), False

    func = getattr(actuators, name, None)
    result = func(**args) if func else f"Error: Unknown tool {name}"
    activity = actuators._consume_activity()
    status = "failed" if result.startswith("Error:") else "success"
    return _emit_result(result, status, activity), False
