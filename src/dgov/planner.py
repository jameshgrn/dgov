# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai",
#     "rich",
# ]
# ///

"""DGOV Planner: read-only agent that explores a codebase and emits structured plans."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

from dgov.llm_backoff import create_chat_completion_with_backoff  # noqa: E402
from dgov.worker import (  # noqa: E402
    WorkerEvent,
    _execute_tool_call,
    _iteration_budget,
    _repo_map_snapshot,
    _resolve_config,
)
from dgov.workers.atomic import AtomicTools, get_allowed_tool_names, get_tool_spec  # noqa: E402


def _build_system_prompt(worktree: Path, config: Any, interactive: bool = False) -> str:
    """Construct the planner's system prompt."""
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

    interactive_section = ""
    if interactive:
        interactive_section = """
INTERACTIVE MODE:
- You have access to the `ask_user` tool. Use it to resolve ambiguity.
- Ask ONE question at a time. Include your recommended answer with each question.
- Do not batch questions. Wait for each answer before asking the next.
- Ask questions when: the goal is ambiguous, there are multiple valid approaches,
  you need domain knowledge the code does not reveal, or you are unsure which
  files are in scope.
- Do not ask questions you can answer by reading the code. Explore first, ask second.
"""
    else:
        interactive_section = """
AUTONOMOUS MODE:
- You cannot ask questions. Make your best judgment based on the codebase.
- When uncertain, prefer the simpler, more conservative approach.
- Note any assumptions in the plan summary.
"""

    sections = [
        f"""[DGOV_PLANNER_PROMPT_V1.0.0]

Greetings, Planner.

You are operating inside a dedicated Sandbox (project root: {worktree}).
Your mission is to explore this codebase, understand the problem, and produce
a structured implementation plan that downstream workers can execute surgically.

THE DGOV WAY:
- You are the Architect. Workers are the Implementers. Settlement is the Auditor.
- Your plan is the contract. Workers will follow it literally — they cannot see
  what you see, only what you write in each task prompt.
- Every task you define must be self-contained: its prompt, file claims, and commit
  message must be sufficient for a worker that has never seen this conversation.
""",
        rules_context,
        project_section,
        interactive_section,
        f"\nREPO MAP:\n{repo_map}",
        f"""
ENVIRONMENT:
- Python: {sys.executable}
- Available: rg, sg (ast-grep), jq, tree, git, python, pytest, ruff (all pre-installed)
- Everything is pre-installed. Do NOT install packages, create venvs, or pip install.
- Use relative paths for all file tools.

PLANNING CONTRACT:
- You are read-only by construction. Editing tools are intentionally unavailable.
- Your sole output mechanism is the `emit_plan` tool.
- A good plan has 1-5 tasks. Prefer fewer, larger tasks over many small ones.
- Each task must claim exactly the files it will touch. Unclaimed files cause
  scope violations at settlement (terminal, no retry).
- Task prompts MUST follow the Orient/Edit/Verify structure:
  Orient: Tell the worker what to read first and what patterns to look for.
  Edit: Describe the exact changes — which functions, what logic, what to add/remove.
  Verify: Tell the worker how to check their work (run tests, check syntax, git diff).
- Commit messages must be imperative mood, one logical change per task.
- Dependencies (depends_on) express real ordering constraints only.
  Independent tasks run in parallel — do not add false dependencies.
- If a task creates a new file, use files.create. If it modifies an existing file,
  use files.edit. Use files.touch when you know a file is involved but the action
  is ambiguous. Use files.read for context-only files the worker should examine.

CONFIG OVERRIDES:
- If you discover that the project uses different tooling than what is configured
  (e.g., different test command, different linter), include config_overrides in
  your emit_plan call. Supported keys: src_dir, test_dir, lint_cmd, format_cmd,
  lint_fix_cmd, test_cmd, language.
- Only include overrides you have evidence for. Do not guess.

WORKFLOW:
1. ORIENT: Start with the repo map. Use tree, list_dir, and read_file to understand
   the project structure, build system, and conventions. Read config files
   (pyproject.toml, package.json, setup.cfg, etc.) to understand tooling.
2. ANALYZE: Read the source files relevant to the goal. Trace call chains,
   understand data flow, identify the minimal set of files that need to change.
   Use find_references, ast_grep, and ripgrep to map the impact surface.
3. DECOMPOSE: Split the work into tasks. Each task should produce one logical,
   reviewable change. Consider:
   - Can tasks run in parallel? (no shared file claims, no ordering dependency)
   - Does a later task depend on an earlier task's output?
4. DRAFT: Write each task's prompt as if briefing a skilled developer who has
   never seen this repo. Include Orient/Edit/Verify sections with concrete
   file paths, function names, and expected behavior.
5. EMIT: Call `emit_plan` with the complete plan. Double-check that:
   - Every file referenced in a prompt is claimed in that task's files.
   - No two independent tasks claim the same file.
   - Commit messages are imperative and specific.

ITERATION BUDGET:
- You have a healthy budget of {config.worker_iteration_budget} tool calls.
- Tactical Check-in: If you find yourself past call
  {config.worker_iteration_warn_at} without a clear decomposition,
  stop exploring new areas and synthesize what you have.
- Do NOT loop on the same dead end more than 3 times.

DO NOT:
- Edit code. You are read-only.
- Produce vague task prompts like "fix the bug" or "update the code."
  Every prompt must specify WHICH code, WHAT change, and HOW to verify.
- Create tasks for things that are already working.
- Over-decompose. A 2-line change does not need 5 tasks.
- Claim files speculatively. Only claim files you have read and confirmed
  need changes.
- Ignore test files. If a code change needs test updates, include them.
- Return without calling emit_plan. Your only valid exit is a complete plan.
""",
        "Strictly use tools. Call 'emit_plan' when your plan is ready.",
    ]
    return "".join(sections)


def _ask_user_via_stdin(question: str) -> str:
    """Emit question event, block on stdin for answer from CLI."""
    WorkerEvent("question", question).emit()
    line = sys.stdin.readline().strip()
    if not line:
        return "(no answer provided)"
    try:
        data = json.loads(line)
        return data.get("answer", "(no answer)")
    except json.JSONDecodeError:
        return line


def run_planner(
    goal: str,
    worktree: Path,
    model: str,
    project_config_json: str = "",
    interactive: bool = False,
) -> None:
    """Run the planner agent loop."""
    config = _resolve_config(worktree, project_config_json)
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        WorkerEvent("error", f"{config.llm_api_key_env} missing").emit()
        sys.exit(1)

    client = OpenAI(base_url=config.llm_base_url, api_key=api_key, max_retries=0)
    actuators = AtomicTools(worktree, config)

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    ask_fn = _ask_user_via_stdin if interactive else None

    messages: list[Any] = [
        {"role": "system", "content": _build_system_prompt(worktree, config, interactive)},
        {"role": "user", "content": goal},
    ]
    nudged = False
    allowed_tools = get_allowed_tool_names("planner", interactive=interactive)
    budget = _iteration_budget(config)

    for iteration in range(budget):
        try:
            resp = create_chat_completion_with_backoff(
                client,
                model=model,
                messages=messages,
                tools=get_tool_spec("planner", interactive=interactive),
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
                            "You MUST call the `emit_plan` tool to finish. If the task "
                            "is unclear, call `emit_plan` with a best-effort plan and "
                            "note the ambiguity in the summary. Do NOT respond with text only."
                        ),
                    })
                    continue
                WorkerEvent("error", "Agent stopped without calling 'emit_plan'").emit()
                _cleanup()
                sys.exit(1)
            continue

        for tool_index, call in enumerate(msg.tool_calls, start=1):
            result, is_done = _execute_tool_call(
                call,
                actuators,
                allowed_tools=allowed_tools,
                ask_user_fn=ask_fn,
                role="planner",
                turn_index=iteration + 1,
                tool_index=tool_index,
            )
            if is_done:
                _cleanup()
                sys.exit(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.function.name,
                "content": result,
            })

    WorkerEvent("error", f"Exceeded max iterations ({budget})").emit()
    _cleanup()
    sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--model", default="accounts/fireworks/routers/kimi-k2p5-turbo")
    parser.add_argument("--project-config", default="", help="JSON-encoded project config")
    parser.add_argument("--interactive", action="store_true", help="Enable ask_user tool")
    args = parser.parse_args()
    run_planner(
        args.goal,
        Path(args.worktree),
        args.model,
        args.project_config,
        interactive=args.interactive,
    )
