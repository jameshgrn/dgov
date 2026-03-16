# Hooks Compatibility Audit

Summary of compatibility between Claude Code hooks, dgov lifecycle hooks, and worker operations.

## Summary Table

| Hook | Classification | Trigger | Interference / Failure Modes |
| :--- | :--- | :--- | :--- |
| **Claude Code Hooks** | | | |
| `commit-msg-lint.sh` | **FIX** | `git commit -m` | Blocks commits with subject >72 chars or non-imperative mood ("Added..."). Workers often use past tense. |
| `targeted-pytest.sh` | **FIX** | `git commit` | Blocks commits if tests fail for changed files. Can stall workers in broken environments or during complex refactors. |
| `no-merge-guard.sh` | **WARN** | `git merge` | Blocks non-FF merges. Skips in worktrees, but can false-positive if "git merge" appears in strings. |
| `dep-drift-guard.sh` | **WARN** | `git commit` | Blocks if `pyproject.toml` is staged without `uv.lock`. Workers must be instructed to run `uv lock`. |
| `docs-codespell.sh` | **WARN** | `git commit` | Blocks if spelling errors found in docs. Typos in worker commits can stall progress. |
| `branch-naming.sh` | **OK** | `git checkout -b` | Skips in worktrees (where workers operate). |
| `dirty-tree-guard.sh` | **OK** | `git rebase/merge` | Skips in worktrees. |
| `ruff-format.sh` | **OK** | `Write` (Python) | Auto-formats on write. Generally helpful, matches dgov standards. |
| `secrets-scanner.sh` | **OK** | `Write/Edit` | Blocks commit of secrets. Essential safety. |
| `staged-file-guard.sh`| **OK** | `git add/commit` | Blocks large/sensitive files. Essential safety. |
| **dgov Global Hooks** | | | |
| `worktree_created` | **OK** | Worker start | Correctly scaffolds `CLAUDE.md` with agent-specific instructions. |
| `pre_merge` | **OK** | `dgov merge` | Restores protected files (`CLAUDE.md`) before merging to main. |
| `post_merge` | **OK** | `dgov merge` | Auto-lints and amends the merge commit. |
| `before_worktree_remove` | **OK** | Worker cleanup | Archives artifacts and `HANDOVER.md`. |
| **dgov Project Hooks** | | | |
| `pre-merge-commit` | **FIX** | `git merge/pull` | Blocks workers from integrating. Essential boundary enforcement. |

## Detailed Findings

### 1. Claude Code Hook: `commit-msg-lint.sh`
- **Behavior**: Enforces ≤ 72 character subjects and imperative mood (blocks "added", "fixed", "implemented", etc.).
- **Trigger**: Fires on any `git commit -m` command executed via the Claude CLI.
- **Issue**: Workers frequently use past tense in commit messages (e.g., "Added tests for X"). This hook will hard-fail the commit, causing the worker to stall or retry indefinitely.
- **Classification**: **FIX**.
- **Recommendation**: Update worker instructions in `worktree_created` to emphasize imperative mood, or disable this hook in worktrees.

### 2. Claude Code Hook: `targeted-pytest.sh`
- **Behavior**: Identifies tests related to changed `.py` files and runs them before allowing a commit.
- **Trigger**: `git commit`.
- **Issue**: If tests fail, the commit is blocked. While this ensures quality, it can block a worker who is committing a "partial fix" or working in an environment where unrelated tests are failing. It also adds significant latency to every commit.
- **Classification**: **FIX**.
- **Recommendation**: Disable in worker worktrees to allow workers to commit and let the Governor/CI handle validation.

### 3. Claude Code Hook: `no-merge-guard.sh`
- **Behavior**: Blocks `git merge` unless `--ff-only` is used.
- **Trigger**: `git merge`.
- **Issue**: Although it includes a check to skip in worktrees (`[[ "$GIT_DIR" != "$GIT_COMMON" ]]`), the user reports it can false-positive if the string "git merge" appears anywhere in the command (e.g., in a `dgov` prompt passed as an argument).
- **Classification**: **WARN**.
- **Recommendation**: Refine the grep pattern to ensure it only matches the start of a command.

### 4. dgov Worker Hook: `pre-merge-commit`
- **Behavior**: Hard-blocks `git merge`, `git pull`, and `git rebase` with an error message.
- **Trigger**: Executed as a pre-tool hook or manually in worktrees.
- **Issue**: Correctly enforces the "Workers stay on their branch" rule. It will intentionally "break" a worker if they try to be too clever with git.
- **Classification**: **OK/FIX** (Working as intended).

### 5. dgov Global Hook: `worktree_created`
- **Behavior**: Writes a tailored `CLAUDE.md` to the worktree and sets it to `--assume-unchanged` so the worker starts with a clean `git status`.
- **Trigger**: Fired by `dgov pane create`.
- **Issue**: None found. It correctly identifies the project type (Python/JS/Rust/Go) and provides appropriate tool commands.
- **Classification**: **OK**.

## Environment Check: Hook Execution Context
Claude Code hooks (in `~/.claude/hooks/`) are a feature of the `claude` CLI.
- **If agent is `claude`**: These hooks **will** fire inside the tmux pane if the worker uses the `claude` CLI to execute commands.
- **If agent is `pi`**: These hooks **will NOT** fire, as `pi` (Qwen) uses a different execution engine that does not load Claude's hook configuration.
- **Impact**: This creates inconsistent behavior between agents. `claude` might be blocked by a lint error that `pi` ignores.

## Conclusion
The primary risks are `commit-msg-lint.sh` and `targeted-pytest.sh`. These should be modified to skip in worktrees or be disabled for automated worker sessions to prevent unnecessary stalls.
