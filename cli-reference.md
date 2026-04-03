# CLI reference

This page documents every command and flag available in the `dgov` CLI.

## Global options

These options appear on many commands. They are listed once here and omitted from per-command tables unless the short flag differs.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root`| `-r` | string | `.` | Project root |
| `--session-root`| `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |

---

> Running `dgov` inside the repo already sets `--project-root` to `.` by default, so you only need `-r` when invoking the CLI from outside the repo or when you want to be explicit about a different root.

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

### dgov plan refactor

Generate a structured prompt for a refactoring task (Move, Extract, Inline, etc.).

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--src` | | string | required | Source (e.g. `src/a.py:func`) |
| `--dest`| | string | required | Destination file (e.g. `src/b.py`) |
| `--task`| | string | `Move` | Action type |

### dgov plan scratch

Create an ephemeral plan skeleton under `.dgov/plans/`. Scratch plans are
gitignored by design and intended for draft orchestration work, not checked-in
repo artifacts.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `NAME` | | string | required | Scratch plan name; writes `.dgov/plans/<name>.toml` |
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |
| `--force` | | bool | `False` | Overwrite an existing scratch plan |

### dgov plan validate

Parse a plan TOML file and print validation issues.

### dgov plan compile

Compile a plan TOML file into a DAG and render the tier view without executing it.

### dgov plan run

Execute a validated plan through the canonical DAG pipeline. This is the primary dispatch surface for all implementation work.

### dgov rebase

Rebase the governor's branch onto its upstream. Stashes dirty changes, rebases, pops stash. Aborts on conflict.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--onto` | | string | `None` | Explicit base branch (default: auto-detect upstream or main) |

### dgov dashboard

Launch a live terminal dashboard showing pane status, agents, and health. The dashboard is an event-driven observer; `--refresh` controls UI repaint cadence, not orchestration polling.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |
| `--refresh` | | float | `2` | Refresh interval in seconds |

### dgov tunnel

Establish or refresh the River SSH multiplexed tunnel. Uses `zsh` to source `~/.zshrc` and run the `river-tunnel` function. This is recommended if local River workers fail preflight checks.


---

## OpenRouter integration

### dgov openrouter status

Show API key status, default model, and connectivity to OpenRouter. No arguments.

### dgov openrouter models

List available free models on OpenRouter. No arguments.

### dgov openrouter test

Send a test prompt to OpenRouter and show the response.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--prompt` | `-p` | string | `Say hello in one word.` | Test prompt |
| `--model` | `-m` | string | `None` | Model to use (default: account default) |

---

## Pane observation

These commands are for **observing and recovering** panes. The kernel handles dispatch, monitoring, and merge automatically via `dgov plan run`.

### dgov pane list

List all worker panes with live status. Displays a formatted table by default.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | | bool | `False` | Output as JSON |

### dgov pane classify

Classify a task and recommend an agent (OpenRouter or local Qwen 4B).

**Arguments**: `PROMPT`

### dgov pane review

Preview a worker pane's changes. Shows diff stat, protected file check, safe-to-merge verdict.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--full` | | bool | `False` | Show complete diff (not just stat) |

### dgov pane diff

Show git diff for a worker pane's branch vs base commit.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--stat` | | bool | `False` | Show diffstat only |
| `--name-only`| | bool | `False` | Show changed file names only |

### dgov pane output

Show clean worker output. Prefers live tmux capture for TUI agents, falls back to persistent log for dead panes.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `-n` | | int | `50` | Number of lines to show |

### dgov pane tail

Stream live output from a running pane.

**Arguments**: `SLUG`

---

## Pane recovery

### dgov pane signal

Manually signal a pane as done or failed. Used for recovery when automatic detection fails.

**Arguments**: `SLUG`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--done` | | bool | `False` | Mark pane as done |
| `--failed` | | bool | `False` | Mark pane as failed |

### dgov pane recover

Rebuild state from events after unexpected termination or database corruption.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |

---

## Pane cleanup

### dgov pane close

Close a worker pane: kill tmux pane, remove worktree. This is a recovery operation — the kernel normally handles cleanup automatically.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Remove worktree even if dirty |

### dgov gc

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

---

## Checkpoints

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

Show which agent/pane last touched a file. Resolves commits to agents via merge SHA lookup and subject line parsing. Supports line-level blame for inspecting specific line ranges.

**Arguments**: `FILE_PATH`

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-R` | string | `None` | Session root |
| `--all` | `-a` | bool | `False` | Show full history (not just last touch) |
| `--agent` | | string | `None` | Filter by agent name |
| `--line-level` | | bool | `False` | Show line-level blame detail |
| `--lines` | `-L` | string | `None` | Line range for line-level blame (e.g. `10-20` or `10`) |

Note: `blame` uses `-R` for `--session-root`, not `-S`.

### dgov preflight

Run pre-flight checks before dispatch. Standalone version of the checks that run automatically during `dgov plan run`.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root` | `-r` | string | `.` | Project root |
| `--session-root` | `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |
| `--agent` | `-a` | string | `claude` | Agent to validate for |
| `--fix` | | bool | `False` | Auto-fix fixable failures |
| `--touches` | `-t` | string | `None` | Files the task will touch (repeatable) |
| `--branch` | `-b` | string | `None` | Expected branch name |
