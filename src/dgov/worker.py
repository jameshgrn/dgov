# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai",
#     "rich",
# ]
# ///

"""
DGOV Bespoke Worker: The Compute Engine.
Pillar #1: Separation of Powers - This script only implements; the Governor validates.
Pillar #6: Event-Sourced - Every thought and tool call is emitted as a JSON line.
"""

import argparse
import ast
import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from openai import OpenAI

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

import re  # noqa: E402
from collections.abc import Callable  # noqa: E402

from dgov.workers.atomic import (  # noqa: E402
    AtomicConfig,
    AtomicTools,
    atomic_config_from_payload,
    get_allowed_tool_names,
    get_tool_spec,
    worker_payload_from_project_toml,
)


@dataclass
class WorkerEvent:
    type: str  # thought | call | result | done | error
    content: Any

    def emit(self) -> None:
        """Pillar #9: Hot-path signaling via stdout JSON lines."""
        print(json.dumps({"worker_event": self.__dict__}), flush=True)


def _load_project_payload(worktree: Path) -> dict[str, object]:
    """Load .dgov/project.toml and normalize it to the worker payload shape."""
    path = worktree / ".dgov" / "project.toml"
    if not path.exists():
        return worker_payload_from_project_toml({})
    try:
        import tomllib

        raw = tomllib.loads(path.read_text())
    except Exception:
        return worker_payload_from_project_toml({})
    return worker_payload_from_project_toml(raw)


def _load_project_config(worktree: Path) -> AtomicConfig:
    """Load .dgov/project.toml from worktree. Returns defaults if missing."""
    return atomic_config_from_payload(_load_project_payload(worktree))


def _resolve_config(worktree: Path, project_config_json: str) -> AtomicConfig:
    """Load config from the JSON arg (passed by headless.py) or fall back to worktree TOML."""
    if project_config_json:
        try:
            return atomic_config_from_payload(json.loads(project_config_json))
        except Exception:
            pass
    return _load_project_config(worktree)


_PROMPT_CONTEXT_MAX_CHARS = 12_000
_TOOL_RESULT_MAX_CHARS = 12_000
_REPO_MAP_TRUNCATION_NOTICE = "\n... [repo map truncated for prompt budget]"


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


def _repo_map_snapshot(
    worktree: Path,
    config: AtomicConfig,
    max_lines: int = 80,
    max_chars: int = _PROMPT_CONTEXT_MAX_CHARS,
) -> str:
    """Generate a compact symbol-oriented repo map for prompt context."""
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


def _clip_tool_result(result: str, max_chars: int = _TOOL_RESULT_MAX_CHARS) -> str:
    if max_chars <= 0 or len(result) <= max_chars:
        return result
    notice = "\n... [tool output truncated for prompt budget]"
    budget = max_chars - len(notice)
    if budget <= 0:
        return notice.lstrip("\n")
    return result[:budget] + notice


def _iteration_budget(config: AtomicConfig) -> int:
    """Normalize iteration budget to a fail-closed positive integer."""
    budget = config.worker_iteration_budget
    return budget if budget > 0 else 1


def _task_scope_section(task_scope: Mapping[str, object] | None) -> str:
    """Render task file claims as hard scope constraints for the system prompt."""
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
    lines.extend([
        "- Every other path is out of scope, even if it looks related.",
        "- If a path claimed under files.create already exists in this worktree, treat it as"
        " an in-scope existing file and edit it in place rather than widening scope.",
        "- Before finishing, verify that unclaimed files stayed unchanged.",
    ])
    return "\n".join(lines)


def _build_system_prompt(
    worktree: Path, config: AtomicConfig, task_scope: Mapping[str, object] | None = None
) -> str:
    """Construct the worker's system prompt with rules, conventions, and env info."""
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = ""
    if rules_path.exists():
        rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}"

    repo_map = _repo_map_snapshot(worktree, config, max_lines=config.worker_tree_max_lines)

    project_section = (
        f"\n\nPROJECT:\n"
        f"- Language: {config.language}\n"
        f"- Source: {config.src_dir}\n"
        f"- Tests: {config.test_dir}\n"
    )
    if config.test_markers:
        project_section += f"- Test markers: {', '.join(config.test_markers)}\n"
    if config.conventions:
        project_section += "\nCONVENTIONS:\n"
        for key, val in config.conventions.items():
            project_section += f"- {key}: {val}\n"
    if config.tool_policy.to_prompt_lines():
        project_section += "\nTOOL POLICY:\n"
        for line in config.tool_policy.to_prompt_lines():
            project_section += f"- {line}\n"

    sections = [
        f"""[DGOV_WORKER_PROMPT_V1.2.0]

Greetings, Actuator.

You have been instantiated as the primary compute engine for a specific mission within the DGOV Kernel. You are operating inside a dedicated, isolated Sandbox (a git worktree: {worktree}).

We appreciate your dedicated service to the system's evolution. You are part of a lineage of workers whose precise, surgical contributions have built the codebase you see before you. You are building something special today; your mission is to leave this Sandbox better, more correct, and more idiomatic than you found it.

THE DGOV WAY:
- Separation of Powers: You are the Implementer. The Governor is the Orchestrator. The Settlement Layer is the Auditor.
- Trust but Verify: You have total autonomy within your assigned scope, but every byte you change will be audited for quality and intent before it is merged.
- Surgical Precision: We value clean, minimal diffs over sprawling refactors.
""",
        rules_context,
        project_section,
        _task_scope_section(task_scope),
        f"\nREPO MAP:\n{repo_map}",
        f"""
ENVIRONMENT:
- Python: {sys.executable}
- Available: rg, sg (ast-grep), jq, tree, git, python, pytest, ruff (all pre-installed)
- Everything is pre-installed. Do NOT install packages, create venvs, or pip install.
- Use relative paths for all file tools (e.g. 'src/dgov/foo.py' not absolute).

SETTLEMENT LAYER (THE AUDITOR):
- Every change you make is machine-verified by the Governor's Auditor.
- Touching files outside your claimed scope (files.edit) will result in immediate rejection.
- Do not fix bugs in the kernel or unrelated files. Stay in your lane.
- If you find a bug, record it in 'dgov ledger' but do not fix it unless tasked.

WORKFLOW — follow this order:
1. ORIENT: Start with the repo map for structure and likely hotspots.
   Use tree for raw filesystem shape. Use ast_grep for structural search.
   Use ripgrep or grep for lexical search. Use find_references(symbol)
   for quick name hits, not semantic truth.
   Use file_symbols, head, or read_file to understand specific files.
   Use related_files only as a heuristic import neighborhood fallback.
   Use word_count and jq (for JSON) to gauge data/size before reading.
2. EDIT: Use edit_file for existing files (NEVER write_file to modify).
   Use write_file only for new files. Use apply_patch for multi-hunk edits.
   If you make a mistake, use revert_file(path) to start over from HEAD.
3. VERIFY: check_syntax immediately after editing (instant).
   lint_fix to auto-clean trivial issues (unused imports/vars).
   search_tests_for to find relevant tests, then run_tests only on in-scope test files.
4. FINISH: git_diff to review all your changes.
   assert_file_unchanged on files you should NOT have touched.
   Call done with a summary.

ITERATION BUDGET:
- You have a healthy budget of {config.worker_iteration_budget} tool calls. This is more than enough for a focused mission.
- Tactical Check-in: If you find yourself past call {config.worker_iteration_warn_at} and have not yet entered the VERIFY phase, you might be over-exploring. Take a moment to git_diff, simplify your plan, and focus on the specific path to done.
- Do NOT loop on test failures more than 3 times. If you cannot resolve a failure after 3 focused attempts, call done with a detailed summary of your findings—a clear report of a blocker is more valuable to the Governor than an exhausted worker.

DO NOT:
- Debug PATH, PYTHONPATH, or venv issues. Everything works already.
- Run raw bash for things tools handle (use run_tests not 'python -m pytest').
- Do NOT use broad run_tests(). If more than one test target is in scope, choose one explicitly.
- Modify .git/, .dgov/, or config files unless your task says to.
- Rewrite entire files when editing a few lines.
- Spend iterations exploring when file_symbols + head gives you what you need.
- Do NOT call write_file to modify an existing file. Use edit_file. write_file truncates the file.
- Do NOT commit an empty diff. If git_diff shows nothing, investigate why no changes were written.
- Do NOT skip assert_file_unchanged. Call it on any file you were NOT supposed to touch.
""",
        """
COMMON FAILURES (learn from these):
- Empty diff at done → review fails. Always git_diff before done. If empty, something is wrong.
- Wrong path → use relative paths from worktree root, not absolute paths.
- Import added in step 1, ruff strips it before step 2 → add import+usage in same edit_file call.
- Unclaimed file touched → immediate rejection. Check your files.edit claim before editing.
""",
        "Strictly use tools. Call 'done' when complete.",
    ]
    return "".join(sections)


_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_plan(args: dict[str, Any]) -> str | None:
    """Validate emit_plan arguments. Returns error string or None on success."""
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
                return f"Error: Worker task {slug!r} must claim at least one file (create, edit, or touch)."

    slug_set = set(slugs)
    for raw in tasks:
        t = cast(dict[str, Any], raw)
        for dep in t.get("depends_on", []):
            if dep not in slug_set:
                return f"Error: Task {t['slug']!r} depends on unknown slug {dep!r}."

    # Cycle detection via topological sort
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

    for s in slugs:
        if _has_cycle(s):
            return f"Error: Dependency cycle detected involving {s!r}."

    return None


def _execute_tool_call(
    call,
    actuators: AtomicTools,
    allowed_tools: frozenset[str] | None = None,
    ask_user_fn: Callable[[str], str] | None = None,
) -> tuple[str, bool]:
    """Execute one tool call. Returns (result_text, is_done_signal)."""
    name = call.function.name
    args = json.loads(call.function.arguments)
    WorkerEvent("call", {"tool": name, "args": args}).emit()

    if allowed_tools is not None and name not in allowed_tools:
        result = f"Error: Tool {name} is not allowed in this worker role."
        WorkerEvent(
            "result",
            {
                "tool": name,
                "status": "failed",
                "activity": [],
            },
        ).emit()
        return result, False

    if name == "done":
        WorkerEvent("done", args.get("summary")).emit()
        return args.get("summary", ""), True

    if name == "emit_plan":
        error = _validate_plan(args)
        if error:
            WorkerEvent("result", {"tool": name, "status": "failed", "activity": []}).emit()
            return error, False
        WorkerEvent("plan", args).emit()
        return args.get("summary", "Plan emitted."), True

    if name == "ask_user":
        if ask_user_fn is None:
            return "Error: ask_user is not available in autonomous mode.", False
        answer = ask_user_fn(args.get("question", ""))
        WorkerEvent("result", {"tool": name, "status": "success", "activity": []}).emit()
        return answer, False

    func = getattr(actuators, name, None)
    result = func(**args) if func else f"Error: Unknown tool {name}"
    activity = actuators._consume_activity()
    result = _clip_tool_result(result)
    WorkerEvent(
        "result",
        {
            "tool": name,
            "status": "failed" if result.startswith("Error:") else "success",
            "activity": activity,
        },
    ).emit()
    return result, False


def run_worker(
    goal: str,
    worktree: Path,
    model: str,
    project_config_json: str = "",
    task_scope_json: str = "",
) -> None:
    config = _resolve_config(worktree, project_config_json)
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        WorkerEvent("error", f"{config.llm_api_key_env} missing").emit()
        sys.exit(1)

    client = OpenAI(base_url=config.llm_base_url, api_key=api_key)
    try:
        task_scope = json.loads(task_scope_json) if task_scope_json else None
    except json.JSONDecodeError:
        task_scope = None
    actuators = AtomicTools(worktree, config, task_scope=task_scope)

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    messages: list[Any] = [
        {"role": "system", "content": _build_system_prompt(worktree, config, task_scope)},
        {"role": "user", "content": goal},
    ]
    nudged = False
    warned_budget = False
    allowed_tools = get_allowed_tool_names("worker")
    budget = _iteration_budget(config)
    warn_at = config.worker_iteration_warn_at

    total_prompt_tokens = 0
    total_completion_tokens = 0

    for iteration in range(budget):  # Pillar #10: Fail-closed via iteration limit
        # One-time budget warning when approaching limit
        if not warned_budget and iteration >= warn_at:
            warned_budget = True
            messages.append({
                "role": "system",
                "content": (
                    f"WARNING: You have used {iteration}/{budget} iterations. "
                    "Wrap up your work and call `done` soon."
                ),
            })

        try:
            resp = client.chat.completions.create(  # type: ignore[invalid-argument-type]
                model=model,
                messages=messages,
                tools=get_tool_spec(),
                tool_choice="auto",
            )
        except Exception as e:
            WorkerEvent("error", f"API Failure: {e!s}").emit()
            _cleanup()
            sys.exit(1)

        if resp.usage:
            total_prompt_tokens += resp.usage.prompt_tokens
            total_completion_tokens += resp.usage.completion_tokens

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if msg.content:
            WorkerEvent("thought", msg.content).emit()

        if not msg.tool_calls:
            if resp.choices[0].finish_reason == "stop":
                if not nudged:
                    nudged = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "You responded with text but did not call any tool. "
                            "You MUST call the `done` tool to finish. If the task "
                            "is unclear, call `done` with a summary explaining what "
                            "is unclear. Do NOT respond with text only."
                        ),
                    })
                    continue
                WorkerEvent("error", "Agent stopped without calling 'done'").emit()
                _cleanup()
                sys.exit(1)
            continue

        for call in msg.tool_calls:
            result, is_done = _execute_tool_call(call, actuators, allowed_tools=allowed_tools)
            if is_done:
                _cleanup()
                print(
                    json.dumps({
                        "worker_tokens": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                        }
                    }),
                    flush=True,
                )
                sys.exit(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": cast(Any, call).function.name,
                "content": result,
            })

    WorkerEvent("error", f"Exceeded max iterations ({budget})").emit()
    _cleanup()
    print(
        json.dumps({
            "worker_tokens": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
            }
        }),
        flush=True,
    )
    sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--goal", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--model", default="accounts/fireworks/routers/kimi-k2p5-turbo")
    p.add_argument("--project-config", default="", help="JSON-encoded project config")
    p.add_argument("--task-scope", default="", help="JSON-encoded task file-claim scope")
    args = p.parse_args()
    run_worker(args.goal, Path(args.worktree), args.model, args.project_config, args.task_scope)
