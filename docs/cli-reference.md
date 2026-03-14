# CLI reference

This page documents every command and flag available in the `dgov` CLI.

## Global options

These options appear on many commands. They are listed once here and omitted from per-command tables unless the short flag differs.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root`| `-r` | string | `.` | Project root |
| `--session-root`| `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |

---

## General

### dgov (bare)

Start or attach to a per-repo tmux session. Inside tmux, styles the session and governor pane. Outside tmux, creates the session and attaches.

### dgov status

Get full dgov status as JSON (panes, agents, health).

### dgov agents

List all registered agents and their install/health status.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root for registry loading |

### dgov version

Show dgov version. No arguments.

### dgov rebase

Rebase the governor's branch onto its upstream. Stashes dirty changes, rebases, pops stash. Aborts on conflict.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--onto` | | string | `None` | Explicit base branch (default: auto-detect upstream or main) |

---

## Pane lifecycle

### dgov pane create

Create a worker pane: worktree + tmux pane + agent.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `None` | Agent to launch (or `auto` to classify) |
| `--prompt` | `-p` | string | `None` | Task prompt |
| `--permission-mode`| `-m`| string | `acceptEdits` | Mode: `plan`, `acceptEdits`, `bypassPermissions` |
| `--slug` | `-s` | string | `None` | Override auto-generated slug |
| `--extra-flags` | `-f` | string | `""` | Extra flags for the agent CLI |
| `--env` | `-e` | string | `None` | Environment variable as `KEY=VALUE` (repeatable) |
| `--preflight` | | bool | `True` | Run pre-flight checks before dispatch |
| `--fix` | | bool | `True` | Auto-fix preflight failures |
| `--max-retries` | | int | `None` | Override agent max retries (0=disable) |
| `--template` | `-T` | string | `None` | Use a prompt template by name |
| `--var` | | string | `None` | Template variable as `key=value` (repeatable) |

### dgov pane list

List all worker panes with live status. Displays a formatted table by default.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | | bool | `False` | Output as JSON |

### dgov pane wait

Wait for a single worker pane to finish. Three detection modes (first wins): done-signal file, new commits beyond base SHA, output stabilization.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--timeout` | `-t` | int | `600` | Max seconds to wait (0 = forever) |
| `--poll` | `-i` | int | `3` | Poll interval in seconds |
| `--stable` | `-s` | int | `15` | Seconds of stable output before declaring done |
| `--auto-retry` | | bool | `True` | Auto-retry failed panes per agent retry policy |

### dgov pane wait-all

Wait for ALL active worker panes. Prints each result as it completes.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--timeout` | `-t` | int | `600` | Max seconds to wait (0 = forever) |
| `--poll` | `-i` | int | `3` | Poll interval in seconds |
| `--stable` | `-s` | int | `15` | Seconds of stable output before declaring done |

### dgov pane review

Preview a worker pane's changes before merging. Shows diff stat, protected file check, safe-to-merge verdict.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--full` | | bool | `False` | Show complete diff (not just stat) |

### dgov pane diff

Show git diff for a worker pane's branch vs base commit.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--stat` | | bool | `False` | Show diffstat only |
| `--name-only`| | bool | `False` | Show changed file names only |

### dgov pane capture

Capture the last N lines of a worker pane's live tmux output.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--lines` | `-n` | int | `30` | Number of lines to capture |

### dgov pane merge

Merge a worker branch into main with configurable conflict resolution.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--close` | | bool | `True` | Close worker pane after merge |
| `--resolve` | | string | `agent` | Conflict resolution: `agent` or `manual` |

### dgov pane merge-all

Merge ALL done worker panes sequentially. Prints combined summary with merged/failed counts.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--close` | | bool | `True` | Close worker panes after merge |
| `--resolve` | | string | `agent` | Conflict resolution strategy |

---

## Pane recovery

### dgov pane escalate

Re-dispatch a pane's task to a different (stronger) agent.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `claude` | Agent to escalate to |
| `--permission-mode`| `-m`| string | `acceptEdits` | Permission mode for the new agent |

### dgov pane retry

Retry a failed pane with a new attempt. Creates a new pane with an attempt suffix; original is marked `superseded`.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `None` | Override agent for retry |
| `--prompt` | `-p` | string | `None` | Override prompt for retry |
| `--permission-mode`| `-m`| string | `acceptEdits` | Permission mode |

### dgov pane resume

Re-launch an agent in an existing worktree (no new branch or worktree created).

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `None` | Override agent |
| `--prompt` | `-p` | string | `None` | Override prompt |
| `--permission-mode`| `-m`| string | `acceptEdits` | Permission mode |

---

## Pane communication

### dgov pane interact

Send a message to a worker pane via tmux send-keys.

**Arguments**: `SLUG`, `MESSAGE`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--session-root` | `-S` | string | `None` | Location of `.dgov/` |

### dgov pane respond

Send a response to a worker pane. Alias for `interact`.

**Arguments**: `SLUG`, `MESSAGE`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--session-root` | `-S` | string | `None` | Location of `.dgov/` |

### dgov pane nudge

Nudge a worker: ask if done and parse a YES/NO response.

**Arguments**: `SLUG`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--session-root` | `-S` | string | `None` | Location of `.dgov/` |
| `--wait` | `-w` | int | `10` | Seconds to wait for response |

### dgov pane signal

Manually signal a pane as done or failed.

**Arguments**: `SLUG`, `SIGNAL_TYPE` (`done` or `failed`)

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--session-root` | `-S` | string | `None` | Location of `.dgov/` |

---

## Pane cleanup

### dgov pane close

Close a worker pane: kill tmux pane, remove worktree.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Remove worktree even if dirty |

### dgov pane prune

Remove stale pane entries (dead pane + no worktree). No arguments beyond global options.

### dgov pane logs

Show persistent log for a pane (written via tmux pipe-pane).

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--tail` | `-n` | int | `None` | Show last N lines only |

---

## Utility panes

### dgov pane util

Run an arbitrary command in a utility pane (no worktree created).

**Arguments**: `COMMAND`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--title` | `-t` | string | `None` | Pane title (defaults to command name) |
| `--cwd` | `-c` | string | `.` | Working directory |

### dgov pane lazygit

Launch lazygit in a utility pane.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--cwd` | `-c` | string | `.` | Working directory |

### dgov pane yazi

Launch yazi in a utility pane.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--cwd` | `-c` | string | `.` | Working directory |

### dgov pane htop

Launch htop in a utility pane.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--cwd` | `-c` | string | `.` | Working directory |

### dgov pane k9s

Launch k9s in a utility pane.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--cwd` | `-c` | string | `.` | Working directory |

### dgov pane top

Launch btop in a utility pane.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--cwd` | `-c` | string | `.` | Working directory |

---

## Batch & checkpoints

### dgov batch

Execute a batch spec (JSON) with DAG-ordered parallelism. Tasks with disjoint `touches` run in parallel tiers.

**Arguments**: `SPEC_PATH`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--session-root` | `-S` | string | `None` | Location of `.dgov/` |
| `--dry-run` | | bool | `False` | Show computed tiers without executing |

### dgov checkpoint create

Create a named checkpoint of current state (state.db + events.jsonl snapshot).

**Arguments**: `NAME`

### dgov checkpoint list

List all checkpoints. No arguments beyond global options.

---

## Templates

### dgov template list

List all available templates (built-in + user).

### dgov template show

Show a template's details: description, body, required variables, default agent.

**Arguments**: `NAME`

### dgov template create

Create a new template TOML file in `.dgov/templates/`.

**Arguments**: `NAME`

No flags. Uses current directory as session root.

---

## Experiments

### dgov experiment start

Run an iterative experiment loop: dispatch worker, evaluate metric, accept or reject.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--program` | `-p` | path | required | Program file (markdown) |
| `--metric` | `-m` | string | required | Metric name to optimize |
| `--budget` | `-b` | int | `5` | Max number of experiments |
| `--agent` | `-a` | string | `claude` | Agent to use |
| `--direction`| `-d` | string | `minimize`| `minimize` or `maximize` |
| `--timeout` | `-t` | int | `600` | Timeout per experiment in seconds |
| `--dry-run` | | bool | `False` | Show plan without executing |

### dgov experiment log

Show the experiment log as JSON for a given program.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--program` | `-p` | string | required | Program name (stem of program file) |

### dgov experiment summary

Show summary stats (best metric, run count, direction) for an experiment program.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--program` | `-p` | string | required | Program name (stem of program file) |
| `--direction`| `-d` | string | `minimize`| `minimize` or `maximize` |

---

## Review-fix pipeline

### dgov review-fix

Run the review-then-fix pipeline: review targets, collect findings, optionally dispatch fixes.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--targets` | `-t` | string | required | File/directory paths to review (repeatable) |
| `--review-agent` | | string | `claude` | Agent for the review phase |
| `--fix-agent` | | string | `claude` | Agent for the fix phase |
| `--auto-approve` | | bool | `False` | Dispatch fixes automatically |
| `--severity` | | string | `medium` | Threshold: `critical`, `medium`, `low` |
| `--timeout` | | int | `600` | Timeout per phase in seconds |

---

## Inspection

### dgov blame

Show which agent/pane last touched a file. Resolves commits to agents via merge SHA lookup and subject line parsing.

**Arguments**: `FILE_PATH`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-R` | string | `None` | Session root |
| `--all` | `-a` | bool | `False` | Show full history (not just last touch) |
| `--agent` | | string | `None` | Filter by agent name |

Note: `blame` uses `-R` for `--session-root`, not `-S`.

### dgov preflight

Run pre-flight checks before dispatch. Standalone version of the checks that run automatically during `pane create`.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `claude` | Agent to validate for |
| `--fix` | | bool | `False` | Auto-fix fixable failures |
| `--touches` | `-t` | string | `None` | Files the task will touch (repeatable) |
| `--branch` | `-b` | string | `None` | Expected branch name |
