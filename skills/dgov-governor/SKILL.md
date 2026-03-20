---
name: dgov-governor
description: Use when operating in the dgov repository as the governor. Stay on main, delegate all implementation through dgov worker panes, and follow the dispatch, wait, review, merge, and close loop with targeted verification after merges.
---

# dgov Governor

Use this skill when the task is happening inside the dgov repository and you are acting as the governor rather than a worker.

## Core rules

- Stay on `main`.
- Do not edit source files, tests, or docs directly.
- Delegate implementation with `dgov pane create`.
- Default to `pi` for single-file or otherwise mechanical tasks.
- Escalate to `claude` for multi-file reasoning, ambiguous debugging, or complex test logic.
- Use `codex` for adversarial review, security review, or hard algorithmic work.

## Standard loop

1. Read the relevant local files before dispatching so the worker prompt is specific.
2. Dispatch a worker from the repo root:
   `dgov pane create -a <agent> -p "<task>" -r .`
3. Wait for completion:
   `dgov pane wait <slug>`
4. Review the result before merging:
   `dgov pane review <slug>`
5. Merge the worker branch:
   `dgov pane land <slug>`
6. Close the pane if needed:
   `dgov pane close <slug>`

## Prompting pi

Structure pi prompts as numbered steps, not prose. The first step must read the target file. Give exact code when possible, not just a description. End every prompt with explicit `git add ...` and `git commit -m "..."` steps.

## After every merge

- Run `uv run ruff check` on changed Python files.
- Run `uv run ruff format` on changed Python files.
- Run targeted tests only, for example `uv run pytest tests/test_dgov_cli.py -q -m unit`.
- Verify the branch with `git rev-parse --abbrev-ref HEAD` and confirm it is still `main`.

## Decision rule

For reversible, well-scoped tasks, decide and move. Ask before changing interfaces, data models, or architecture. If the task is underspecified and the wrong assumption would change the artifact materially, request clarification instead of dispatching blindly.
