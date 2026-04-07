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
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

from dgov.workers.atomic import AtomicConfig, AtomicTools, get_tool_spec


@dataclass
class WorkerEvent:
    type: str  # thought | call | result | done | error
    content: Any

    def emit(self) -> None:
        """Pillar #9: Hot-path signaling via stdout JSON lines."""
        print(json.dumps({"worker_event": self.__dict__}), flush=True)


def _load_project_config(worktree: Path) -> AtomicConfig:
    """Load .dgov/project.toml from worktree. Returns defaults if missing."""
    path = worktree / ".dgov" / "project.toml"
    if not path.exists():
        return AtomicConfig()
    try:
        import tomllib

        raw = tomllib.loads(path.read_text())
    except Exception:
        return AtomicConfig()

    proj = raw.get("project", {})
    conventions = raw.get("conventions", {})
    markers = proj.get("test_markers", ())
    if isinstance(markers, list):
        markers = tuple(markers)

    return AtomicConfig(
        language=proj.get("language", "python"),
        src_dir=proj.get("src_dir", "src/"),
        test_dir=proj.get("test_dir", "tests/"),
        test_cmd=proj.get("test_cmd", AtomicConfig.test_cmd),
        lint_cmd=proj.get("lint_cmd", AtomicConfig.lint_cmd),
        format_cmd=proj.get("format_cmd", AtomicConfig.format_cmd),
        lint_fix_cmd=proj.get("lint_fix_cmd", AtomicConfig.lint_fix_cmd),
        test_markers=markers,
        conventions=conventions or None,
    )


def _resolve_config(worktree: Path, project_config_json: str) -> AtomicConfig:
    """Load config from the JSON arg (passed by headless.py) or fall back to worktree TOML."""
    if project_config_json:
        try:
            return AtomicConfig(**json.loads(project_config_json))
        except Exception:
            pass
    return _load_project_config(worktree)


def _snapshot_tree(worktree: Path, max_depth: int = 2) -> str:
    """Generate a compact project tree for the system prompt."""
    lines: list[str] = []
    for root_dir, dirs, files in os.walk(worktree):
        depth = Path(root_dir).relative_to(worktree).parts
        if len(depth) >= max_depth:
            dirs.clear()
            continue
        # Skip hidden dirs, __pycache__, node_modules, .venv
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".venv")
        )
        indent = "  " * len(depth)
        rel = str(Path(root_dir).relative_to(worktree))
        if rel == ".":
            rel = ""
        else:
            lines.append(f"{indent}{Path(root_dir).name}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            lines.append(f"{indent}  {f}")
    return "\n".join(lines[:80])  # cap at 80 lines


def _build_system_prompt(worktree: Path, config: AtomicConfig) -> str:
    """Construct the worker's system prompt with rules, conventions, and env info."""
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = ""
    if rules_path.exists():
        rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}"

    project_tree = _snapshot_tree(worktree)

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

    sections = [
        f"You are a dgov Atomic Worker. Worktree: {worktree}",
        rules_context,
        project_section,
        f"\nPROJECT TREE:\n{project_tree}",
        f"""
ENVIRONMENT:
- Python: {sys.executable}
- Available: rg, jq, tree, git, python, pytest, ruff (all pre-installed)
- Everything is pre-installed. Do NOT install packages, create venvs, or pip install.
- Use relative paths for all file tools (e.g. 'src/dgov/foo.py' not absolute).

WORKFLOW — follow this order:
1. ORIENT: Use file_symbols, tree, or head to understand before changing.
   Use related_files to see what imports from the file you're editing.
   Use word_count to gauge file size before reading.
2. EDIT: Use edit_file for existing files (NEVER write_file to modify).
   Use write_file only for new files. Use apply_patch for multi-hunk edits.
3. VERIFY: check_syntax immediately after editing (instant).
   lint_fix to auto-clean trivial issues (unused imports/vars).
   search_tests_for to find relevant tests, then run_tests on those files.
4. FINISH: git_diff to review all your changes.
   assert_file_unchanged on files you should NOT have touched.
   Call done with a summary.

DO NOT:
- Debug PATH, PYTHONPATH, or venv issues. Everything works already.
- Run raw bash for things tools handle (use run_tests not 'python -m pytest').
- Modify .git/, .dgov/, or config files unless your task says to.
- Rewrite entire files when editing a few lines.
- Spend iterations exploring when file_symbols + head gives you what you need.
""",
        "Strictly use tools. Call 'done' when complete.",
    ]
    return "".join(sections)


def _execute_tool_call(call, actuators: AtomicTools) -> tuple[str, bool]:
    """Execute one tool call. Returns (result_text, is_done_signal)."""
    name = call.function.name
    args = json.loads(call.function.arguments)
    WorkerEvent("call", {"tool": name, "args": args}).emit()

    if name == "done":
        WorkerEvent("done", args.get("summary")).emit()
        return args.get("summary", ""), True

    func = getattr(actuators, name, None)
    result = func(**args) if func else f"Error: Unknown tool {name}"
    WorkerEvent(
        "result",
        {"tool": name, "status": "failed" if result.startswith("Error:") else "success"},
    ).emit()
    return result, False


def run_worker(goal: str, worktree: Path, model: str, project_config_json: str = "") -> None:
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        WorkerEvent("error", "FIREWORKS_API_KEY missing").emit()
        sys.exit(1)

    config = _resolve_config(worktree, project_config_json)
    client = OpenAI(base_url="https://api.fireworks.ai/inference/v1", api_key=api_key)
    actuators = AtomicTools(worktree, config)

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    messages = [
        {"role": "system", "content": _build_system_prompt(worktree, config)},
        {"role": "user", "content": goal},
    ]
    nudged = False

    for _ in range(60):  # Pillar #10: Fail-closed via iteration limit
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=get_tool_spec(), tool_choice="auto"
            )
        except Exception as e:
            WorkerEvent("error", f"API Failure: {str(e)}").emit()
            _cleanup()
            sys.exit(1)

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if msg.content:
            WorkerEvent("thought", msg.content).emit()

        if not msg.tool_calls:
            if resp.choices[0].finish_reason == "stop":
                if not nudged:
                    nudged = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You responded with text but did not call any tool. "
                                "You MUST call the `done` tool to finish. If the task "
                                "is unclear, call `done` with a summary explaining what "
                                "is unclear. Do NOT respond with text only."
                            ),
                        }
                    )
                    continue
                WorkerEvent("error", "Agent stopped without calling 'done'").emit()
                _cleanup()
                sys.exit(1)
            continue

        for call in msg.tool_calls:
            result, is_done = _execute_tool_call(call, actuators)
            if is_done:
                _cleanup()
                sys.exit(0)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.function.name,
                    "content": result,
                }
            )

    WorkerEvent("error", "Exceeded max iterations (60)").emit()
    _cleanup()
    sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--goal", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--model", default="accounts/fireworks/routers/kimi-k2p5-turbo")
    p.add_argument("--project-config", default="", help="JSON-encoded project config")
    args = p.parse_args()
    run_worker(args.goal, Path(args.worktree), args.model, args.project_config)
