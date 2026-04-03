# Napkin

## Corrections
| Date | Source | What Went Wrong | What To Do Instead |
|------|--------|----------------|-------------------|

## User Preferences
- Uses `uv` exclusively, never pip/poetry
- `ruff check` + `ruff format` for Python linting
- `shellcheck` + `shfmt -i 2` for bash scripts
- `actionlint` for GitHub Actions workflows
- Feature branches only, never push to main
- Imperative mood commit messages, <=72 chars
- Absolute file paths in output (Ghostty compatibility)

## Patterns That Work
- Hook scripts: `set -euo pipefail`, read JSON from stdin via `jq`, exit 0 pass / exit 2 block
- Existing hooks in `~/.claude/settings.json` use both inline commands and external scripts
- `CLAUDE_PROJECT_DIR` env var available in hooks for project-relative paths
- Worktree detection: `git rev-parse --git-dir != --git-common-dir` means inside a worktree
- dmux manages worktrees/branches/merges — hooks for git state should skip in worktrees

## Patterns That Don't Work
- `uv run codespell` fails in repos without pyproject.toml — use `uvx codespell` instead

## Domain Notes
- Hook system lives in `~/.claude/settings.json` under `hooks` key
- Hook matchers: `Bash`, `Edit|Write`, `Write`
- PreToolUse Bash (15 hooks):
  - rm-rf guard (inline), main-push guard (inline)
  - enforce-uv.sh, commit-msg-lint.sh
  - env-sanity.sh, uv-run-enforcer.sh
  - no-merge-guard.sh, branch-naming.sh
  - conflict-marker-guard.sh, debug-statement-guard.sh
  - dirty-tree-guard.sh, staged-file-guard.sh, dep-drift-guard.sh
  - docs-codespell.sh, targeted-pytest.sh
- PreToolUse Edit|Write (3 hooks):
  - protected-file guard (inline), secrets-scanner.sh, generated-dir-guard.sh
- PostToolUse Edit|Write (6 hooks):
  - ruff format/check, shfmt/shellcheck, actionlint
  - whitespace-fix.sh, structured-validate.sh, auto-chmod-sh.sh
- Order matters: cheap pattern-match first → git-status checks → expensive (codespell, pytest)
- GitHub Actions templates at `~/.github/workflows/` (claude_pr_review.yml, automerge.yml)
- PR helper scripts at `~/scripts/` (pr-create-and-return.sh, pr-return.sh)
- `CLAUDE_PROJECT_DIR` env var available in hooks for project-relative paths
- env-sanity.sh exits early if no `.venv/` in project — won't fire outside Python projects
