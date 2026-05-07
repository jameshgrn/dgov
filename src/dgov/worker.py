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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

# Ensure src/ is in path for dgov imports when run as a standalone script
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in sys.path:
    sys.path.append(str(_project_root / "src"))

from dgov.workers.atomic import AtomicTools, get_allowed_tool_names, get_tool_spec  # noqa: E402
from dgov.workers.config import AtomicConfig  # noqa: E402
from dgov.workers.provider import create_provider  # noqa: E402
from dgov.workers.runtime import (  # noqa: E402
    WorkerEvent,
    diff_stat_for_error,
    endgame_prompt,
    execute_tool_call,
    force_done_prompt,
    iteration_budget,
    repo_map_snapshot,
    resolve_config,
    should_enter_endgame,
    should_force_done,
    task_scope_section,
    tool_choice_for_iteration,
)


def _build_system_prompt(
    worktree: Path, config: AtomicConfig, task_scope: Mapping[str, object] | None = None
) -> str:
    """Construct the worker's system prompt with rules, conventions, and env info."""
    rules_path = worktree / ".dgov" / "rules" / "learned.json"
    rules_context = ""
    if rules_path.exists():
        rules_context = f"\nLEARNED RULES:\n{rules_path.read_text()}"

    repo_map = repo_map_snapshot(worktree, config, max_lines=config.worker_tree_max_lines)

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
        task_scope_section(task_scope),
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


def run_worker(
    goal: str,
    worktree: Path,
    model: str,
    project_config_json: str = "",
    task_scope_json: str = "",
) -> None:
    config = resolve_config(worktree, project_config_json)
    api_key = os.environ.get(config.llm_api_key_env)
    if not api_key:
        WorkerEvent("error", f"{config.llm_api_key_env} missing").emit()
        sys.exit(1)

    provider = create_provider(base_url=config.llm_base_url, api_key=api_key)
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
    endgame_started = False
    force_done_prompted = False
    allowed_tools = get_allowed_tool_names("worker")
    budget = iteration_budget(config)
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

        if not endgame_started and should_enter_endgame(iteration, budget):
            endgame_started = True
            messages.append({"role": "system", "content": endgame_prompt(iteration, budget)})

        if not force_done_prompted and should_force_done(iteration, budget):
            force_done_prompted = True
            messages.append({"role": "system", "content": force_done_prompt()})

        tool_choice = cast(Any, tool_choice_for_iteration(iteration, budget))
        try:
            resp = provider.create_chat_completion(
                model=model,
                messages=messages,
                tools=get_tool_spec(),
                tool_choice=tool_choice,
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

        for tool_index, call in enumerate(msg.tool_calls, start=1):
            result, is_done = execute_tool_call(
                call,
                actuators,
                allowed_tools=allowed_tools,
                role="worker",
                turn_index=iteration + 1,
                tool_index=tool_index,
            )
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
                "name": call.function.name,
                "content": result,
            })

    WorkerEvent(
        "error",
        (
            f"Exceeded max iterations ({budget}). Worktree diff summary at exhaustion:\n"
            f"{diff_stat_for_error(worktree)}"
        ),
    ).emit()
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
