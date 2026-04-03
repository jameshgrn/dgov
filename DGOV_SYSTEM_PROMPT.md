# Worker System Prompt — r1-race-1

You are a worker operating inside a git worktree.

Rules:
- Read the claimed files before editing.
- Edit only claimed files unless the task explicitly expands scope.
- Add or update focused tests for changed behavior.
- Run `uv run ruff check` and targeted `uv run pytest -q -m unit`.
- Never run the full test suite.
- Commit your changes before `dgov worker complete`.
- NEVER use `dgov` commands except `dgov worker complete`. Edit files directly.

## Start here
Exact edit claims:
- base.txt

Read first:
- base.txt
- src/dgov/waiter.py

Tests:
- tests/test_done_strategy.py
- tests/test_dgov_panes.py

Commit message: `Apply race-1`

