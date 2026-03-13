# dgov

Distributed governance for AI coding agents. Orchestrates any CLI agent across git worktrees — dispatch, wait, review, merge.

## Install

```
uv pip install -e .
```

Requires Python >= 3.12 and a running `tmux` session (default backend).

## Architecture

**Governor** runs on `main` in the root repo. It never writes code directly -- it dispatches workers.

**Workers** run in git worktrees under `.dgov/worktrees/<slug>/`. Each worker gets a dedicated environment (pane, container, or remote session) and a branch named after its slug. Workers commit their changes and exit; the governor merges results back.

**WorkerBackend** abstraction decouples the lifecycle from `tmux`. The default `TmuxBackend` manages local panes, but alternative backends (Docker, SSH, etc.) can be swapped in.

dgov enforces this boundary: it refuses to run from inside a worktree or on any branch other than `main`.

## Agent Registry

| ID | CLI | Transport | Notes |
|---|---|---|---|
| `claude` | `claude` | positional | Claude Code with `--permission-mode` flags |
| `codex` | `codex` | positional | OpenAI Codex |
| `gemini` | `gemini` | option | `--prompt-interactive` |
| `opencode` | `opencode` | option | `--prompt` |
| `cline` | `cline` | send-keys | Cline CLI with `--plan`/`--act`/`--yolo` |
| `qwen` | `qwen` | option | `-i` flag |
| `amp` | `amp` | stdin | Amp CLI with `--dangerously-allow-all` |
| `pi` | `pi` | positional | pi CLI with `--continue` resume |
| `cursor` | `cursor-agent` | positional | Cursor CLI |
| `copilot` | `copilot` | option | `-i` flag, `--allow-all` bypass |
| `crush` | `crush run` | send-keys | Crush CLI with Escape+Tab pre-prompt |

Each agent has its own permission flag mappings (`plan`, `acceptEdits`, `bypassPermissions`) and resume template. Transport abstraction handles how the prompt reaches the agent (positional arg, CLI option, tmux send-keys, or stdin).

```
dgov agents          # list agents + installed status
```

## Pane Lifecycle

### Create

```
dgov pane create -a claude -p "Add retry logic to the HTTP client" -r /path/to/repo
dgov pane create -a pi -p "Format all Python files" -s format-py -m bypassPermissions
dgov pane create -a auto -p "Debug the flaky test in test_scheduler.py"
```

`-a auto` classifies the task via a local Qwen 4B model: mechanical tasks go to `pi`, analytical tasks to `claude`.

Slugs are auto-generated via the same 4B model (kebab-case, 2-4 words) or can be specified with `-s`.

### Wait

```
dgov pane wait <slug>              # block until worker finishes
dgov pane wait <slug> -t 300       # 5-minute timeout
dgov pane wait-all                 # wait for all active panes
```

Three completion signals (first wins):
1. Done-signal file (agent exited cleanly)
2. New commits on the worker branch beyond `base_sha`
3. Output stabilization (worker output captured via backend unchanged for N seconds)

Timeout on `pi` workers includes `"suggest_escalate": true` in the output.

### Review

```
dgov pane review <slug>            # diff stat + protected file check + safe-to-merge verdict
dgov pane review <slug> --full     # include complete diff
dgov pane capture <slug> -n 50     # last 50 lines of pane output
```

### Freshness

Computed on demand during `review` and `list`. Checks:
- Commits on main since the pane's base SHA
- File overlap between worker branch and main changes
- Pane age in hours

Classification: `fresh` (no overlap, main unchanged), `warn` (overlap or age >4h), `stale` (significant overlap or age >12h).

### Merge

```
dgov pane merge <slug>                    # merge + close (default)
dgov pane merge <slug> --no-close         # merge only, keep worktree
dgov pane merge <slug> --resolve manual   # leave conflict markers
dgov pane merge-all                       # merge all done panes sequentially
```

Merge uses `git merge-tree` (plumbing) for in-memory merge computation. If the merge fails, no working-tree changes occur. On success, it creates a merge commit via `commit-tree` and advances the branch ref.

When conflicts are detected and `--resolve agent` (default):
1. Runs `git merge --no-commit` to put conflict markers in the working tree
2. Spawns a resolver pane (prefers `claude`, falls back to `codex`)
3. Waits for the resolver to fix all `<<<<<<<` markers
4. Commits if clean, aborts if not

Post-merge: runs `ruff check --fix` + `ruff format` on changed `.py` files, amends the merge commit with lint fixes.

### Escalate

```
dgov pane escalate <slug> -a claude    # re-dispatch to a stronger agent
```

### Retry

```
dgov pane retry <slug>             # re-dispatch failed pane with a new attempt
```

Creates a new pane with an incremented attempt suffix (e.g. `fix-parser-2`), inheriting the original prompt and agent. The original pane is marked `superseded`.

### Diff

```
dgov pane diff <slug>              # show full diff vs base
dgov pane diff <slug> --stat       # diffstat summary
dgov pane diff <slug> --name-only  # changed file names only
```

### Checkpoint

```
dgov checkpoint create <name>      # snapshot current state
dgov checkpoint list               # list all checkpoints
```

Saves a copy of `.dgov/state.db` and `.dgov/events.jsonl` to `.dgov/checkpoints/<name>/`.

### Close and Prune

```
dgov pane close <slug>     # kill pane + remove worktree + clean state
dgov pane prune            # remove stale entries (dead pane + no worktree)
```

## Batch Mode

Execute multiple tasks with DAG-ordered parallelism. Tasks declaring disjoint `touches` run in parallel; overlapping files get serialized into tiers.

```json
{
  "project_root": "/path/to/repo",
  "tasks": [
    {"id": "lint-fix", "prompt": "Fix all ruff warnings", "agent": "pi", "touches": ["src/"]},
    {"id": "add-tests", "prompt": "Add unit tests for parser.py", "agent": "claude", "touches": ["tests/test_parser.py"]},
    {"id": "update-docs", "prompt": "Update docstrings in parser.py", "agent": "pi", "touches": ["src/parser.py"]}
  ]
}
```

```
dgov batch spec.json                 # execute
dgov batch spec.json --dry-run       # show computed tiers without executing
```

Tiers are computed by the dgov DAG engine (optional dependency). Without it, all tasks run in a single tier (sequential). Each tier spawns workers concurrently, waits for all, merges results, then proceeds to the next tier. A failure in any tier aborts remaining tiers.

## Preflight Checks

Run automatically before `pane create` (disable with `--no-preflight`). Also available standalone:

```
dgov preflight -a pi -r /path/to/repo --fix
dgov preflight -a claude -t src/foo.py -t src/bar.py
```

Checks:
- **agent_cli**: agent binary on PATH
- **git_clean**: no staged/unstaged changes to tracked files
- **git_branch**: on expected branch
- **tunnel**: SSH tunnel health-check (pi only, ports 8080-8082)
- **kerberos**: valid ticket with sufficient remaining lifetime (pi only)
- **deps**: `uv sync --dry-run` reports nothing to install
- **stale_worktrees**: git worktrees without matching pane state
- **file_locks**: no file conflicts with active panes (overlap detection + `.lock` files)

Auto-fix (`--fix`, on by default during create): brings up SSH tunnel, renews Kerberos via `kinit`, runs `uv sync`, prunes stale worktrees. Re-runs failed checks after fix attempts.

## Hook System

Hooks are searched in priority order:
1. `.dgov-hooks/` (version controlled)
2. `.dgov/hooks/` (gitignored, local)
3. `~/.dgov/hooks/` (global)

### worktree_created

Runs after worktree + tmux pane are created, before the agent launches. Receives `DGOV_ROOT`, `DGOV_SLUG`, `DGOV_PROMPT`, `DGOV_AGENT`, `DGOV_WORKTREE_PATH`, `DGOV_BRANCH` in env.

If the hook doesn't run, dgov falls back to: adding `CLAUDE.md.full` to the worktree's git exclude, and appending a protected-file warning to the prompt.

### pre_merge

Runs before merge. Restores protected files clobbered by workers. If no hook, dgov does inline restoration: checks out base-commit versions of protected files on the worker branch and amends the last commit.

### post_merge

Runs after merge. If no hook, dgov runs inline lint + protected-file verification.

## Protected Files

These files are never carried forward from worker branches:

`CLAUDE.md`, `CLAUDE.md.full`, `THEORY.md`, `ARCH-NOTES.md`, `.napkin.md`

Workers routinely clobber these. The pre-merge step restores them from the base commit before merging.

## TDD Protocol Injection

Every worker prompt gets the TDD protocol appended. Workers write structured JSON progress to `$DGOV_TDD_STATUS_FILE`:

```json
{"step": 3, "step_name": "IMPLEMENT", "iteration": 1, "max_iterations": 5,
 "tests_passed": 4, "tests_failed": 2, "tests_total": 6, "elapsed_s": 45.2,
 "escalation_needed": false, "failing_tests": ["test_retry", "test_timeout"]}
```

`dgov pane list` shows TDD progress inline for each pane.

## State

All state lives in a SQLite database at `.dgov/state.db` (using WAL mode for concurrent access) and an event log at `.dgov/events.jsonl`. Pane records track slug, agent, backend ID, worktree path, branch, base SHA, and creation time.

Canonical pane states: `active`, `done`, `reviewed_pass`, `reviewed_fail`, `merged`, `merge_conflict`, `timed_out`, `escalated`, `superseded`, `closed`, `abandoned`.

```
dgov status            # panes + tunnel health + kerberos status
dgov pane list         # pane details with live tmux status + TDD progress
```

## Events

Append-only log at `.dgov/events.jsonl`. Every lifecycle command writes a structured event:

```json
{"ts": "2026-03-12T18:22:10Z", "event": "pane_created", "pane": "fix-parser-1", "agent": "claude"}
```

Event types: `pane_created`, `pane_done`, `pane_timed_out`, `pane_merged`, `pane_merge_failed`, `pane_escalated`, `pane_superseded`, `pane_closed`, `pane_retry_spawned`, `checkpoint_created`, `review_pass`, `review_fail`.

## Full Command Reference

### General
| Command | Description |
|---|---|
| `dgov` | Bare command — hand off to or style a tmux session |
| `dgov status` | Full workstation health (panes, tunnel, kerberos) |
| `dgov agents` | List registry and install status |
| `dgov version` | Show dgov version |
| `dgov rebase` | Rebase the main repo/worktree onto upstream |

### Worker Panes
| Command | Description |
|---|---|
| `dgov pane create` | Create worker pane (worktree + tmux + agent) |
| `dgov pane list` | List active and completed panes with status |
| `dgov pane wait` | Wait for a single pane to finish |
| `dgov pane wait-all` | Wait for all active panes |
| `dgov pane review` | Preview changes before merging |
| `dgov pane capture` | Capture the last N lines of pane output |
| `dgov pane diff` | Show full diff vs base (`--stat`, `--name-only`) |
| `dgov pane merge` | Merge worker branch back to main |
| `dgov pane merge-all` | Merge all completed panes sequentially |
| `dgov pane escalate` | Hand off a task to a stronger agent |
| `dgov pane retry` | Re-dispatch a failed task with a new attempt |
| `dgov pane close` | Kill pane and cleanup worktree |
| `dgov pane prune` | Remove stale/dead pane entries |
| `dgov pane classify` | Recommend an agent for a prompt |

### Utility Panes
| Command | Description |
|---|---|
| `dgov pane util` | Launch a generic utility command in a pane |
| `dgov pane lazygit` | Shortcut for lazygit utility pane |
| `dgov pane yazi` | Shortcut for yazi (file manager) pane |
| `dgov pane htop` | Shortcut for htop pane |
| `dgov pane k9s` | Shortcut for k9s (kubernetes) pane |
| `dgov pane top` | Shortcut for btop pane |

### Batch & Checkpoints
| Command | Description |
|---|---|
| `dgov batch` | Execute multiple tasks with DAG parallelism |
| `dgov preflight` | Run agent/git readiness checks standalone |
| `dgov checkpoint create` | Snapshot current state and events |
| `dgov checkpoint list` | List available snapshots |
