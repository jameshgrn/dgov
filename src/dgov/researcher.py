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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

from dgov.workers.atomic import AtomicTools, get_allowed_tool_names, get_tool_spec  # noqa: E402
from dgov.workers.provider import create_provider  # noqa: E402
from dgov.workers.runtime import (  # noqa: E402
    WorkerEvent,
    execute_tool_call,
    iteration_budget,
    repo_map_snapshot,
    resolve_config,
    task_scope_section,
)


def _rules_context(worktree: Path) -> str:
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    if not rules_path.exists():
        return ""
    return f"\nLEARNED RULES:\n{rules_path.read_text()}"


def _project_section(config: Any) -> str:
    section = (
        f"\n\nPROJECT:\n"
        f"- Language: {config.language}\n"
        f"- Source: {config.src_dir}\n"
        f"- Tests: {config.test_dir}\n"
    )
    if config.test_markers:
        section += f"- Test markers: {', '.join(config.test_markers)}\n"
    if config.conventions:
        section += "\nCONVENTIONS:\n"
        for key, val in config.conventions.items():
            section += f"- {key}: {val}\n"
    if config.tool_policy.to_prompt_lines():
        section += "\nTOOL POLICY:\n"
        for line in config.tool_policy.to_prompt_lines():
            section += f"- {line}\n"
    return section


def _environment_section() -> str:
    return f"""
ENVIRONMENT:
- Python: {sys.executable}
- Available: rg, sg (ast-grep), jq, tree, git, python, pytest, ruff (all pre-installed)
- Everything is pre-installed. Do NOT install packages, create venvs, or pip install.
- Use relative paths for all file tools.
"""


def _research_contract_guidance() -> str:
    return """
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
"""


def _workflow_section() -> str:
    return """
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
"""


def _iteration_budget_section(config: Any) -> str:
    return f"""
ITERATION BUDGET:
- You have a healthy budget of {config.worker_iteration_budget} tool calls.
- Tactical Check-in: If you find yourself past call
  {config.worker_iteration_warn_at} without a coherent findings summary,
  stop exploring new areas and synthesize what you have.
- If the answer is already supported, do not spend remaining budget. Finish early.
- Do NOT loop on the same dead end more than 3 times.
"""


def _do_not_guidance() -> str:
    return """
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
"""


def _research_contract_section(config: Any) -> str:
    return (
        _environment_section()
        + _research_contract_guidance()
        + _workflow_section()
        + _iteration_budget_section(config)
        + _do_not_guidance()
    )


def _build_system_prompt(
    worktree: Path, config: Any, task_scope: Mapping[str, object] | None = None
) -> str:
    """Construct the research worker's system prompt."""
    repo_map = repo_map_snapshot(worktree, config, max_lines=config.worker_tree_max_lines)

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
        _rules_context(worktree),
        _project_section(config),
        task_scope_section(task_scope),
        f"\nREPO MAP:\n{repo_map}",
        _research_contract_section(config),
        "Strictly use tools. Call 'done' when complete.",
    ]
    return "".join(sections)


def _parse_task_scope(task_scope_json: str) -> Any:
    try:
        return json.loads(task_scope_json) if task_scope_json else None
    except json.JSONDecodeError:
        return None


def _initial_messages(
    goal: str,
    worktree: Path,
    config: Any,
    task_scope: Mapping[str, object] | None,
) -> list[Any]:
    return [
        {"role": "system", "content": _build_system_prompt(worktree, config, task_scope)},
        {"role": "user", "content": goal},
    ]


def _create_completion(provider: Any, *, model: str, messages: list[Any]) -> Any:
    return provider.create_chat_completion(
        model=model,
        messages=messages,
        tools=get_tool_spec("researcher"),
        tool_choice="auto",
    )


def _append_assistant_response(messages: list[Any], resp: Any) -> Any:
    msg = resp.choices[0].message
    messages.append(msg.model_dump(exclude_none=True))
    if msg.content:
        WorkerEvent("thought", msg.content).emit()
    return msg


def _handle_missing_tool_call(
    resp: Any,
    messages: list[Any],
    nudged: bool,
    cleanup: Callable[[], None],
) -> bool:
    if resp.choices[0].finish_reason != "stop":
        return nudged
    if not nudged:
        messages.append({
            "role": "user",
            "content": (
                "You responded with text but did not call any tool. "
                "You MUST call the `done` tool to finish. If the task "
                "is unclear, call `done` with a summary explaining what "
                "is unclear. Do NOT respond with text only."
            ),
        })
        return True
    WorkerEvent("error", "Agent stopped without calling 'done'").emit()
    cleanup()
    sys.exit(1)


def _append_tool_message(messages: list[Any], call: Any, result: str) -> None:
    messages.append({
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.function.name,
        "content": result,
    })


def _execute_researcher_tools(
    msg: Any,
    actuators: AtomicTools,
    *,
    allowed_tools: frozenset[str],
    iteration: int,
    messages: list[Any],
) -> bool:
    for tool_index, call in enumerate(msg.tool_calls, start=1):
        result, is_done = execute_tool_call(
            call,
            actuators,
            allowed_tools=allowed_tools,
            role="researcher",
            turn_index=iteration + 1,
            tool_index=tool_index,
        )
        if is_done:
            return True
        _append_tool_message(messages, call, result)
    return False


def _exit_researcher(cleanup: Callable[[], None], code: int) -> None:
    cleanup()
    sys.exit(code)


def _build_runtime_state(
    goal: str,
    worktree: Path,
    project_config_json: str,
    task_scope_json: str,
) -> tuple[Any, Any, AtomicTools, Any, Callable[[], None], list[Any], frozenset[str], int]:
    """Resolve config, initialize provider/actuators, and build initial state.

    Returns: (config, provider, actuators, task_scope, cleanup, messages, allowed_tools, budget)
    """
    config = resolve_config(worktree, project_config_json)
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        WorkerEvent("error", f"{config.llm_api_key_env} missing").emit()
        sys.exit(1)

    provider = create_provider(base_url=config.llm_base_url, api_key=api_key)
    actuators = AtomicTools(worktree, config)
    task_scope = _parse_task_scope(task_scope_json)

    def _cleanup() -> None:
        shutil.rmtree(actuators._sandbox_home, ignore_errors=True)

    messages = _initial_messages(goal, worktree, config, task_scope)
    allowed_tools = get_allowed_tool_names("researcher")
    budget = iteration_budget(config)

    return config, provider, actuators, task_scope, _cleanup, messages, allowed_tools, budget


def run_researcher(
    goal: str,
    worktree: Path,
    model: str,
    project_config_json: str = "",
    task_scope_json: str = "",
) -> None:
    """Run the research worker loop."""
    _, provider, actuators, _, cleanup, messages, allowed_tools, budget = _build_runtime_state(
        goal, worktree, project_config_json, task_scope_json
    )

    nudged = False
    for iteration in range(budget):
        try:
            resp = _create_completion(provider, model=model, messages=messages)
        except Exception as exc:
            WorkerEvent("error", f"API Failure: {exc!s}").emit()
            cleanup()
            sys.exit(1)

        msg = _append_assistant_response(messages, resp)

        if not msg.tool_calls:
            nudged = _handle_missing_tool_call(resp, messages, nudged, cleanup)
            continue

        if _execute_researcher_tools(
            msg,
            actuators,
            allowed_tools=allowed_tools,
            iteration=iteration,
            messages=messages,
        ):
            _exit_researcher(cleanup, 0)

    WorkerEvent("error", f"Exceeded max iterations ({budget})").emit()
    _exit_researcher(cleanup, 1)


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
