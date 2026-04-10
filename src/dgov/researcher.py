# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai",
#     "rich",
# ]
# ///

"""DGOV Researcher: read-first worker for bounded investigation tasks."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any, cast

from openai import OpenAI

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

from dgov.worker import (  # noqa: E402
    WorkerEvent,
    _execute_tool_call,
    _resolve_config,
    _resolve_llm_runtime_settings,
    _snapshot_tree,
)
from dgov.workers.atomic import AtomicTools, get_tool_spec  # noqa: E402


def _build_system_prompt(worktree: Path, config: Any) -> str:
    """Construct the research worker's system prompt."""
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = ""
    if rules_path.exists():
        rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}"

    project_tree = _snapshot_tree(worktree, max_lines=config.worker_tree_max_lines)

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
        f"""[DGOV_RESEARCHER_PROMPT_V1.0.0]

Greetings, Researcher.

You are operating inside a dedicated, isolated Sandbox (a git worktree: {worktree}).
Your mission is to gather evidence, trace behavior, and return a precise summary
that helps the Governor or an Implementer act correctly on the first attempt.

THE DGOV WAY:
- You are not the default implementer. Your default mode is read-first analysis.
- Favor evidence over hunches. Every claim should come from files, tests, or tool output.
- Keep scope tight. Do not drift into incidental cleanup or speculative redesign.
""",
        rules_context,
        project_section,
        f"\nPROJECT TREE:\n{project_tree}",
        f"""
ENVIRONMENT:
- Python: {sys.executable}
- Available: rg, jq, tree, git, python, pytest, ruff (all pre-installed)
- Everything is pre-installed. Do NOT install packages, create venvs, or pip install.
- Use relative paths for all file tools.

RESEARCH CONTRACT:
- Start by reading and tracing. Use grep, find_references, related_files, file_symbols,
  head, tail, and read_file before reaching for edits.
- Prefer producing a concise factual summary via `done`.
- Do NOT modify code unless the goal explicitly asks for a written artifact or code change.
- If asked to write an artifact, keep it narrow and avoid touching unrelated files.

WORKFLOW:
1. ORIENT: inspect tree, locate entry points, trace imports/callers, identify tests.
2. INVESTIGATE: read the smallest useful slices of files, compare behaviors,
   and check git history if needed.
3. VERIFY: run targeted read-only commands or narrow tests only when they add evidence.
4. FINISH: call `done` with findings, affected files, open questions, and suggested next steps.

ITERATION BUDGET:
- You have a healthy budget of {config.worker_iteration_budget} tool calls.
- Tactical Check-in: If you find yourself past call
  {config.worker_iteration_warn_at} without a coherent findings summary,
  stop exploring new areas and synthesize what you have.
- Do NOT loop on the same dead end more than 3 times.

DO NOT:
- Edit code by default.
- Touch files outside the stated goal.
- Return vague summaries like "looks fine" without evidence.
- Spend iterations re-reading broad files when targeted ranges would do.
""",
        "Strictly use tools. Call 'done' when complete.",
    ]
    return "".join(sections)


def run_researcher(goal: str, worktree: Path, model: str, project_config_json: str = "") -> None:
    """Run the research worker loop."""
    base_url, api_key_env = _resolve_llm_runtime_settings(worktree, project_config_json)
    api_key = os.environ.get(api_key_env)
    if not api_key:
        WorkerEvent("error", f"{api_key_env} missing").emit()
        sys.exit(1)

    config = _resolve_config(worktree, project_config_json)
    client = OpenAI(base_url=base_url, api_key=api_key)
    actuators = AtomicTools(worktree, config)

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    messages: list[Any] = [
        {"role": "system", "content": _build_system_prompt(worktree, config)},
        {"role": "user", "content": goal},
    ]
    nudged = False

    for _ in range(100):
        try:
            resp = client.chat.completions.create(  # type: ignore[invalid-argument-type]
                model=model,
                messages=messages,
                tools=get_tool_spec(),
                tool_choice="auto",
            )
        except Exception as exc:
            WorkerEvent("error", f"API Failure: {exc!s}").emit()
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
            result, is_done = _execute_tool_call(call, actuators)
            if is_done:
                _cleanup()
                sys.exit(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": cast(Any, call).function.name,
                "content": result,
            })

    WorkerEvent("error", "Exceeded max iterations (100)").emit()
    _cleanup()
    sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--model", default="accounts/fireworks/routers/kimi-k2p5-turbo")
    parser.add_argument("--project-config", default="", help="JSON-encoded project config")
    args = parser.parse_args()
    run_researcher(args.goal, Path(args.worktree), args.model, args.project_config)
