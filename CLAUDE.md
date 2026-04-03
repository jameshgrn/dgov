# Worker Instructions — lifecycle-slug-history-source-6

You are a **worker**. Complete the task, commit, and signal done.

## Rules
- Edit ONLY the files specified in your task
- Do NOT modify CLAUDE.md, .gitignore, pyproject.toml
- Do NOT create new files unless the task requires it
- Do NOT push to remote
- You are in a git worktree, not the main repo
- CLAUDE.md/AGENTS.md are git-excluded and cannot be committed

## Current Status

**Analysis Complete**: No changes needed. The slug reservation feature is already fully implemented:

1. `slug_history` table exists in DB schema (persistence.py:327)
2. `remove_pane()` records slugs into history (persistence.py:468-470)
3. `_find_unique_slug()` checks active panes AND historical slugs (lifecycle.py:189-195)

**Tests Pass**: All 47 tests in test_lifecycle.py pass, including:
- `test_historical_slug_remains_reserved_after_close` - verifies slugs reserved after close
- `test_slug_allocation_increments_numeric_suffix` - verifies incremental numbering

## Commit Message

No changes to lifecycle.py. The feature was already implemented.