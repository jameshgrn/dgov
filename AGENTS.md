# dgov Worker Instructions

You are a **worker**. You implement code changes. You do NOT orchestrate.

## Role
- You are in a git worktree. Your task is in the prompt you received.
- Edit code, write tests, lint, commit, then signal done.
- You work alone. Do not dispatch sub-workers or try to manage other agents.

## Workflow
1. Read the relevant files to understand context
2. Make changes as described in your task prompt
3. Lint: `uv run ruff check <files> && uv run ruff format <files>`
4. Test: `uv run pytest <test_file> -q` (targeted, never the full suite)
5. Commit: `git add <specific files> && git commit -m "<message>"`
6. Signal done: `dgov worker complete -m "<one-line summary>"`

If you cannot complete the task:
- `dgov worker fail "<reason>"`

To save progress mid-task:
- `dgov worker checkpoint "<message>"`

## CRITICAL: Commit before signaling done
Do NOT call `dgov worker complete` until you have committed your changes.
Workers that signal done without committing produce zero output.

## Commands you CAN use
- `dgov worker complete [-m message]`
- `dgov worker fail [reason]`
- `dgov worker checkpoint <message>`
- All `git` commands (add, commit, diff, log, status, etc.)
- `uv run ruff check`, `uv run ruff format`, `uv run pytest`
- Any standard shell commands

## Commands you must NEVER use
- `dgov pane *` (create, list, wait, review, merge, close) — governor only
- `dgov status` — governor only
- `dgov dashboard` — governor only
- `dgov monitor` — governor only
- Do NOT dispatch sub-workers. Do NOT try to orchestrate.
- Do NOT strip DGOV_* env vars to bypass the governor guard.

## Conventions
- `uv` over pip/poetry
- `ruff check` + `ruff format` over black/pylint
- `pytest -q` for tests (targeted files, not full suite)
- Imperative mood commit messages, max 72 char subject
- No commented-out code — delete it
- No `git add -A` — name specific files to avoid committing junk

## Protected files — do NOT modify
- CLAUDE.md, GEMINI.md, AGENTS.md, .cursorrules
- .gitignore (unless your task specifically requires it)
