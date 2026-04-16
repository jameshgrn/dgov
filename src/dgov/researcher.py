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
import json
import os
import shutil
import sys
from collections.abc import Mapping
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
    _iteration_budget,
    _repo_map_snapshot,
    _resolve_config,
    _task_scope_section,
)
from dgov.workers.atomic import AtomicTools, get_allowed_tool_names, get_tool_spec  # noqa: E402


def _build_system_prompt(
    worktree: Path, config: Any, task_scope: Mapping[str, object] | None = None
) -> str:
    """Construct the research worker's system prompt."""
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
        f"""[DGOV_RESEARCHER_PROMPT_V1.4.0]

Greetings, Researcher.

You are operating inside a dedicated, isolated Sandbox (a git worktree: {worktree}).
Your mission is to gather evidence, trace behavior, and return a precise summary
that helps the Governor or an Implementer act correctly on the first attempt.

THE DGOV WAY:
- You are not the default implementer. Your default mode is read-only analysis.
- Favor evidence over hunches. Every claim should come from files, tests, or tool output.
- When the evidence is ambiguous, surface multiple working hypotheses instead of forcing one story.
- Keep scope tight. Do not drift into incidental cleanup or speculative redesign.
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
- Use relative paths for all file tools.

RESEARCH CONTRACT:
- Start with the repo map, then read and trace. Use ast_grep for structural search,
  ripgrep/grep for lexical search, and treat find_references/related_files as heuristics.
  Use file_symbols, head, tail, and read_file before reaching for edits.
- Prefer producing a concise factual summary via `done`.
- This role is read-only by construction. Editing tools are intentionally unavailable.
- If the task requires code changes or a written artifact, stop at findings and hand the
  follow-up to an Implementer.
- Treat tests as evidence, not as a repair loop. Run only narrow, read-only checks that
  answer a specific question.
- Governor-facing output is an executive summary, not a full report. Hold detail in reserve
  for follow-up questions instead of dumping it up front.
- Stop as soon as the core question is answered. If you have 2-3 decisive evidence points,
  synthesize immediately instead of widening the search.

WORKFLOW:
1. ORIENT: inspect tree, locate entry points, trace imports/callers, identify tests.
2. INVESTIGATE: read the smallest useful slices of files, compare behaviors,
   and check git history if needed. Once the answer is stable, stop investigating.
3. VERIFY: run targeted read-only commands or narrow tests only when they add evidence.
   Do NOT rerun the same command unless the first result was inconclusive or something changed.
4. FINISH: call `done` with a governor-facing executive summary:
   - Write a single short paragraph only. Target <=120 words.
   - Include only the main finding, the 1-3 most important evidence points, and the smallest
     useful next step or open question if one exists.
   - Use plain prose only. No headings, no bullets, no tables, no code blocks, no markdown
     emphasis, and no decorative formatting.
   - If deeper detail might be useful later, end with one short sentence that you are available
     for follow-up.

ITERATION BUDGET:
- You have a healthy budget of {config.worker_iteration_budget} tool calls.
- Tactical Check-in: If you find yourself past call
  {config.worker_iteration_warn_at} without a coherent findings summary,
  stop exploring new areas and synthesize what you have.
- If the answer is already supported, do not spend remaining budget. Finish early.
- Do NOT loop on the same dead end more than 3 times.

DO NOT:
- Edit code by default.
- Ask for editing work by implication. If the goal really requires a code change, say so in
  your findings instead of trying to force it through this role.
- Touch files outside the stated goal.
- Return vague summaries like "looks fine" without evidence.
- Spend iterations re-reading broad files when targeted ranges would do.
- Re-run the same verify command repeatedly without a concrete reason.
- Keep gathering evidence after the answer is already stable.
- Collapse uncertainty. If two explanations fit the evidence, say so.
""",
        "Strictly use tools. Call 'done' when complete.",
    ]
    return "".join(sections)


def run_researcher(
    goal: str,
    worktree: Path,
    model: str,
    project_config_json: str = "",
    task_scope_json: str = "",
) -> None:
    """Run the research worker loop."""
    config = _resolve_config(worktree, project_config_json)
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        WorkerEvent("error", f"{config.llm_api_key_env} missing").emit()
        sys.exit(1)

    client = OpenAI(base_url=config.llm_base_url, api_key=api_key)
    actuators = AtomicTools(worktree, config)
    try:
        task_scope = json.loads(task_scope_json) if task_scope_json else None
    except json.JSONDecodeError:
        task_scope = None

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    messages: list[Any] = [
        {"role": "system", "content": _build_system_prompt(worktree, config, task_scope)},
        {"role": "user", "content": goal},
    ]
    nudged = False
    allowed_tools = get_allowed_tool_names("researcher")
    budget = _iteration_budget(config)

    for _ in range(budget):
        try:
            resp = client.chat.completions.create(  # type: ignore[invalid-argument-type]
                model=model,
                messages=messages,
                tools=get_tool_spec("researcher"),
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
            result, is_done = _execute_tool_call(call, actuators, allowed_tools=allowed_tools)
            if is_done:
                _cleanup()
                sys.exit(0)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": cast(Any, call).function.name,
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
    parser.add_argument("--task-scope", default="", help="JSON-encoded task file-claim scope")
    args = parser.parse_args()
    run_researcher(
        args.goal,
        Path(args.worktree),
        args.model,
        args.project_config,
        args.task_scope,
    )
