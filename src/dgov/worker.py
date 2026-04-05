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
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI


@dataclass
class WorkerEvent:
    type: str  # thought | call | result | done | error
    content: Any

    def emit(self) -> None:
        """Pillar #9: Hot-path signaling via stdout JSON lines."""
        print(json.dumps({"worker_event": self.__dict__}), flush=True)


@dataclass(frozen=True)
class _ProjectConfig:
    """Minimal project config for worker — no dgov imports."""

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    test_markers: tuple[str, ...] = ()
    conventions: dict[str, str] | None = None


def _load_project_config(worktree: Path) -> _ProjectConfig:
    """Load .dgov/project.toml from worktree. Returns defaults if missing."""
    path = worktree / ".dgov" / "project.toml"
    if not path.exists():
        return _ProjectConfig()
    try:
        import tomllib

        raw = tomllib.loads(path.read_text())
    except Exception:
        return _ProjectConfig()

    proj = raw.get("project", {})
    conventions = raw.get("conventions", {})
    markers = proj.get("test_markers", ())
    if isinstance(markers, list):
        markers = tuple(markers)

    return _ProjectConfig(
        language=proj.get("language", "python"),
        src_dir=proj.get("src_dir", "src/"),
        test_dir=proj.get("test_dir", "tests/"),
        test_cmd=proj.get("test_cmd", _ProjectConfig.test_cmd),
        lint_cmd=proj.get("lint_cmd", _ProjectConfig.lint_cmd),
        format_cmd=proj.get("format_cmd", _ProjectConfig.format_cmd),
        test_markers=markers,
        conventions=conventions or None,
    )


class AtomicTools:
    """The Actuator Layer: Strict, isolated tools."""

    def __init__(self, worktree: Path, config: _ProjectConfig) -> None:
        self.worktree = worktree.resolve()
        self.config = config
        # Resolve python/venv paths once at init, not per-command
        self._python_bin = Path(sys.executable).parent
        self._python = sys.executable
        # Sandbox HOME outside worktree — prevents macOS Library/ polluting git status
        self._sandbox_home = Path(tempfile.mkdtemp(prefix="dgov-sandbox-"))

    def _sandbox_env(self) -> dict[str, str]:
        return {
            "PATH": f"{self._python_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._sandbox_home),
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(self.worktree / self.config.src_dir.rstrip("/")),
        }

    def _check_path(self, path: str) -> Path | str:
        """Resolve and validate path is within worktree. Returns Path or error string."""
        target = (self.worktree / path).resolve()
        if not str(target).startswith(str(self.worktree)):
            return "Error: Path traversal attempt blocked."
        return target

    # -- Core tools --

    def read_file(self, path: str) -> str:
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        return target.read_text()

    def write_file(self, path: str, content: str) -> str:
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Successfully wrote {len(content)} bytes to {path}"

    def run_bash(self, cmd: str) -> str:
        """Pillar #7: Zero Ambient Authority - sandboxed execution in worktree."""
        try:
            res = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=self.worktree,
                env=self._sandbox_env(),
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nEXIT:{res.returncode}"
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 60s."

    # -- Navigation tools --

    def grep(self, pattern: str, path: str = ".") -> str:
        """Search file contents by regex pattern. Returns matching lines with file:line prefix."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex: {e}"

        results: list[str] = []
        search_root = target if target.is_dir() else target.parent
        files = [target] if target.is_file() else sorted(search_root.rglob("*"))

        for f in files:
            if not f.is_file() or f.suffix in (".pyc", ".pyo", ".so", ".dylib"):
                continue
            rel = str(f.relative_to(self.worktree))
            if any(part.startswith(".") for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{rel}:{i}: {line}")
                        if len(results) >= 100:
                            results.append("... (truncated at 100 matches)")
                            return "\n".join(results)
            except (UnicodeDecodeError, PermissionError):
                continue

        return "\n".join(results) if results else "No matches found."

    def glob(self, pattern: str) -> str:
        """Find files matching a glob pattern. Returns newline-separated relative paths."""
        results: list[str] = []
        for f in sorted(self.worktree.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(self.worktree))
            if any(part.startswith(".") for part in f.relative_to(self.worktree).parts):
                continue
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                results.append(rel)
                if len(results) >= 200:
                    results.append("... (truncated at 200 files)")
                    break
        return "\n".join(results) if results else "No files matched."

    def list_dir(self, path: str = ".") -> str:
        """List directory contents with type indicators (/ for dirs, sizes for files)."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        if not target.is_dir():
            return f"Error: {path} is not a directory."

        entries: list[str] = []
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            rel = str(item.relative_to(self.worktree))
            if item.is_dir():
                entries.append(f"{rel}/")
            else:
                size = item.stat().st_size
                entries.append(f"{rel}  ({size} bytes)")
        return "\n".join(entries) if entries else "(empty directory)"

    # -- SOP compound tools --

    def run_tests(self, file: str = "") -> str:
        """Run tests using the project's declared test command."""
        cmd = self.config.test_cmd.replace("{test_dir}", self.config.test_dir)
        if file:
            cmd = cmd.replace(self.config.test_dir, file)
        return self.run_bash(cmd)

    def lint_check(self, file: str = "") -> str:
        """Run lint using the project's declared lint command."""
        target = file if file else self.config.src_dir
        cmd = self.config.lint_cmd.replace("{file}", target)
        return self.run_bash(cmd)

    def format_file(self, file: str) -> str:
        """Format a file using the project's declared format command."""
        cmd = self.config.format_cmd.replace("{file}", file)
        return self.run_bash(cmd)


def get_tool_spec() -> list[dict]:
    return [
        # Core tools
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file's contents. Use relative paths (e.g. 'src/foo.py').",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file. Creates parent directories.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Run a shell command in the worktree. 60s timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        },
        # Navigation tools
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Search file contents by regex. Returns file:line: matches.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex pattern"},
                        "path": {
                            "type": "string",
                            "description": "File or directory to search (default: '.')",
                            "default": ".",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Find files matching a pattern (e.g. '*.py', 'tests/test_*.py').",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List directory contents with sizes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory to list (default: '.')",
                            "default": ".",
                        },
                    },
                },
            },
        },
        # SOP tools
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the project's test suite. Optionally target a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specific test file (default: run all)",
                            "default": "",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lint_check",
                "description": "Run the project's linter. Optionally target a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specific file to lint (default: all source)",
                            "default": "",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "format_file",
                "description": "Format a file using the project's formatter.",
                "parameters": {
                    "type": "object",
                    "properties": {"file": {"type": "string"}},
                    "required": ["file"],
                },
            },
        },
        # Exit
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": "Signal that the task is complete.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        },
    ]


def _resolve_config(worktree: Path, project_config_json: str) -> _ProjectConfig:
    """Load config from the JSON arg (passed by headless.py) or fall back to worktree TOML."""
    if project_config_json:
        try:
            return _ProjectConfig(**json.loads(project_config_json))
        except Exception:
            pass
    return _load_project_config(worktree)


def _build_system_prompt(worktree: Path, config: _ProjectConfig) -> str:
    """Construct the worker's system prompt with rules, conventions, and env info."""
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}" if rules_path.exists() else ""

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

    env_info = (
        f"\n\nENVIRONMENT:\n"
        f"- Python: {sys.executable}\n"
        f"- Do NOT install packages or create venvs. Everything is pre-installed.\n"
        f"- Use relative paths for file tools (e.g. 'src/dgov/foo.py' not absolute).\n"
        f"- Use run_tests, lint_check, format_file instead of raw bash for standard ops.\n"
        f"- Use grep, glob, list_dir to navigate the codebase efficiently.\n"
    )

    return (
        f"You are a dgov Atomic Worker. Worktree: {worktree}"
        f"{rules_context}{project_section}{env_info}"
        f"\nStrictly use tools. Call 'done' when complete."
    )


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

    for _ in range(30):  # Pillar #10: Fail-closed via iteration limit
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

    WorkerEvent("error", "Exceeded max iterations (30)").emit()
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
