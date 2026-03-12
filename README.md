# dgov

Programmatic governor CLI for [dmux](https://github.com/anthropics/dmux). Dispatches AI coding agents into isolated git worktrees, manages their lifecycle, and integrates results back to main.

## Install

```
uv pip install -e .
```

Requires Python >= 3.12, a running tmux session, and dmux installed globally.

## Architecture

**Governor** runs on `main` in the root repo. It never writes code directly -- it dispatches workers.

**Workers** run in git worktrees under `.dgov/worktrees/<slug>/`. Each worker gets a tmux pane, an agent CLI, and a branch named after its slug. Workers commit their changes and exit; the governor merges results back.

dgov enforces this boundary: it refuses to run from inside a worktree or on any branch other than `main`.

## Agent Registry

| ID | CLI | Transport | Notes |
|---|---|---|---|
| `claude` | `claude` | positional | Default. Claude Code with `--permission-mode` flags |
| `pi` | `pi` | positional | Qwen 35B via SSH tunnel to river. Free tier |
| `codex` | `codex` | positional | OpenAI Codex |
| `gemini` | `gemini` | option | `--prompt-interactive` |
| `qwen` | `qwen` | option | `-i` flag |

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
3. Output stabilization (tmux capture unchanged for N seconds)

Timeout on `pi` workers includes `"suggest_escalate": true` in the output.

### Review

```
dgov pane review <slug>            # diff stat + protected file check + safe-to-merge verdict
dgov pane review <slug> --full     # include complete diff
dgov pane capture <slug> -n 50     # last 50 lines of pane output
```

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

Tiers are computed by the distributary DAG engine (optional dependency). Without it, all tasks run in a single tier (sequential). Each tier spawns workers concurrently, waits for all, merges results, then proceeds to the next tier. A failure in any tier aborts remaining tiers.

## Preflight Checks

Run automatically before `pane create` (disable with `--no-preflight`). Also available standalone:

```
dgov preflight -a pi -r /path/to/repo --fix
dgov preflight -a claude -t src/foo.py -t src/bar.py
```

Checks:
- **agent_cli**: agent binary on PATH
- **dmux_compat**: installed dmux version matches pin (currently `==5.5.3`)
- **git_clean**: no staged/unstaged changes to tracked files
- **git_branch**: on expected branch
- **tunnel**: SSH tunnel health-check (pi only, ports 8080-8082)
- **kerberos**: valid ticket with sufficient remaining lifetime (pi only)
- **deps**: `uv sync --dry-run` reports nothing to install
- **stale_worktrees**: git worktrees without matching pane state
- **file_locks**: no file conflicts with active panes (overlap detection + `.lock` files)

Auto-fix (`--fix`, on by default during create): brings up SSH tunnel, renews Kerberos via `kinit`, runs `uv sync`, prunes stale worktrees. Re-runs failed checks after fix attempts.

## dmux Version Pinning

dgov pins to an exact dmux version (`==5.5.3`). Any dmux update breaks dgov until the pin is updated. Check compatibility:

```
dgov version
```

## Hook System

Hooks are searched in priority order:
1. `.dmux-hooks/` (version controlled)
2. `.dmux/hooks/` (gitignored, local)
3. `~/.dmux/hooks/` (global)

### worktree_created

Runs after worktree + tmux pane are created, before the agent launches. Receives `DMUX_ROOT`, `DMUX_SLUG`, `DMUX_PROMPT`, `DMUX_AGENT`, `DMUX_WORKTREE_PATH`, `DMUX_BRANCH` in env.

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

Every worker prompt gets the TDD protocol appended. Workers write structured JSON progress to `$DISTRIBUTARY_TDD_STATUS_FILE`:

```json
{"step": 3, "step_name": "IMPLEMENT", "iteration": 1, "max_iterations": 5,
 "tests_passed": 4, "tests_failed": 2, "tests_total": 6, "elapsed_s": 45.2,
 "escalation_needed": false, "failing_tests": ["test_retry", "test_timeout"]}
```

`dgov pane list` shows TDD progress inline for each pane.

## State

All state lives in `.dgov/state.json` (not dmux's config). Pane records track slug, agent, pane ID, worktree path, branch, base SHA, and creation time.

```
dgov status            # panes + tunnel health + kerberos status
dgov pane list         # pane details with live tmux status + TDD progress
```

## Full Command Reference

```
dgov pane create       # create worker pane
dgov pane wait         # wait for single pane
dgov pane wait-all     # wait for all panes
dgov pane review       # preview changes
dgov pane capture      # capture pane output
dgov pane merge        # merge + optional close
dgov pane merge-all    # merge all done panes
dgov pane escalate     # re-dispatch to stronger agent
dgov pane close        # kill pane + cleanup
dgov pane prune        # remove stale entries
dgov pane classify     # classify prompt -> agent recommendation
dgov pane list         # list panes with status
dgov batch             # DAG-ordered batch execution
dgov preflight         # run preflight checks
dgov rebase            # rebase governor onto upstream
dgov status            # full workstation status
dgov agents            # list agent registry
dgov version           # dgov + dmux version + compat check
```
