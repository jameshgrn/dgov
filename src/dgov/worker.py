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
import subprocess
import sys
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


class AtomicTools:
    """The Actuator Layer: Strict, isolated tools."""

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree.resolve()
        # Resolve python/venv paths once at init, not per-command
        self._python_bin = Path(sys.executable).parent
        self._python = sys.executable
        # Sandbox HOME outside worktree — prevents macOS Library/ polluting git status
        import tempfile

        self._sandbox_home = Path(tempfile.mkdtemp(prefix="dgov-sandbox-"))

    def read_file(self, path: str) -> str:
        target = (self.worktree / path).resolve()
        if not str(target).startswith(str(self.worktree)):
            return "Error: Path traversal attempt blocked."
        if not target.exists():
            return f"Error: {path} does not exist."
        return target.read_text()

    def write_file(self, path: str, content: str) -> str:
        target = (self.worktree / path).resolve()
        if not str(target).startswith(str(self.worktree)):
            return "Error: Path traversal attempt blocked."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Successfully wrote {len(content)} bytes to {path}"

    def run_bash(self, cmd: str) -> str:
        """Pillar #7: Zero Ambient Authority - sandboxed execution in worktree."""
        sandbox_env = {
            "PATH": f"{self._python_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._sandbox_home),
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(self.worktree / "src"),
        }
        try:
            res = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=self.worktree,
                env=sandbox_env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nEXIT:{res.returncode}"
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 60s."


def get_tool_spec() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
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
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "done",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        },
    ]


def run_worker(goal: str, worktree: Path, model: str) -> None:
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        WorkerEvent("error", "FIREWORKS_API_KEY missing").emit()
        sys.exit(1)

    client = OpenAI(base_url="https://api.fireworks.ai/inference/v1", api_key=api_key)
    actuators = AtomicTools(worktree)

    # Pillar #2: The Atomic Attempt includes rules injection
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = ""
    if rules_path.exists():
        rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}"

    # Environment info so the worker doesn't waste iterations discovering paths
    python_path = sys.executable
    env_info = (
        f"\n\nENVIRONMENT:\n"
        f"- Python: {python_path}\n"
        f"- Run tests: python -m pytest tests/ -q\n"
        f"- Run linter: python -m ruff check <file>\n"
        f"- Source code: src/ (already on PYTHONPATH)\n"
        f"- Do NOT install packages or create venvs. Everything is pre-installed.\n"
        f"- Use relative paths for file tools (e.g. 'src/dgov/foo.py' not absolute).\n"
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"You are a dgov Atomic Worker. Worktree: {worktree}"
                f"{rules_context}{env_info}"
                f"\nStrictly use tools. Call 'done' when complete."
            ),
        },
        {"role": "user", "content": goal},
    ]

    for step in range(30):  # Pillar #10: Fail-closed via iteration limit
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=get_tool_spec(), tool_choice="auto"
            )
        except Exception as e:
            WorkerEvent("error", f"API Failure: {str(e)}").emit()
            sys.exit(1)

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if msg.content:
            WorkerEvent("thought", msg.content).emit()

        if not msg.tool_calls:
            if resp.choices[0].finish_reason == "stop":
                WorkerEvent("error", "Agent stopped without calling 'done'").emit()
                sys.exit(1)
            continue

        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments)
            WorkerEvent("call", {"tool": name, "args": args}).emit()

            if name == "done":
                WorkerEvent("done", args.get("summary")).emit()
                sys.exit(0)

            # Execute tool
            func = getattr(actuators, name, None)
            result = func(**args) if func else f"Error: Unknown tool {name}"

            WorkerEvent(
                "result",
                {"tool": name, "status": "success" if "Error" not in result else "failed"},
            ).emit()
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "name": name, "content": result}
            )

    WorkerEvent("error", "Exceeded max iterations (30)").emit()
    sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--goal", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--model", default="accounts/fireworks/routers/kimi-k2p5-turbo")
    args = p.parse_args()
    run_worker(args.goal, Path(args.worktree), args.model)
