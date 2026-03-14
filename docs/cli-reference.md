# CLI reference

This page documents every command and flag available in the `dgov` CLI.

## Global options

These options apply to many commands.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--project-root`| `-r` | string | `.` | Project root |
| `--session-root`| `-S` | string | `None` | Location of `.dgov/`. Defaults to project root. |

---

## General commands

### dgov (bare)
Hand off to or style a tmux session.

### dgov status
Get workstation health (panes, tunnel, kerberos) as JSON.

### dgov agents
List all registered agents and their install status.

### dgov version
Show dgov version.

### dgov rebase
Rebase the governor's branch (usually `main`) onto its upstream.

---

## Pane management

### dgov pane create
Create a worker pane.

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
| `--max-retries` | | int | `None` | Override agent max retries |
| `--template` | `-T` | string | `None` | Use a prompt template by name |
| `--var` | | string | `None` | Template variable as `key=value` (repeatable) |

### dgov pane list
List all worker panes with live status.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | | bool | `False` | Output as JSON |

### dgov pane wait
Wait for a single worker to finish.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--timeout` | `-t` | int | `600` | Max seconds to wait (0 = forever) |
| `--poll` | `-i` | int | `3` | Poll interval in seconds |
| `--stable` | `-s` | int | `15` | Seconds of stable output before declaring done |
| `--auto-retry` | | bool | `True` | Auto-retry failed panes per policy |

### dgov pane wait-all
Wait for all currently `active` panes.

### dgov pane review
Preview changes before merging.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--full` | | bool | `False` | Show complete diff (not just stat) |

### dgov pane diff
Show git diff vs base commit.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--stat` | | bool | `False` | Show diffstat only |
| `--name-only`| | bool | `False` | Show changed file names only |

### dgov pane merge
Merge worker branch back to `main`.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--close` | | bool | `True` | Close worker pane after merge |
| `--resolve` | | string | `agent` | Conflict resolution: `agent` or `manual` |

### dgov pane escalate
Re-dispatch to a different agent.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--agent` | `-a` | string | `claude` | Agent to escalate to |
| `--permission-mode`| `-m`| string | `acceptEdits` | Permission mode |

---

## Advanced commands

### dgov batch
Execute a batch spec with DAG-ordered parallelism.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--dry-run` | | bool | `False` | Show computed tiers without executing |

### dgov experiment start
Run an iterative optimization loop.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--program` | `-p` | string | `None` | Program file (markdown) |
| `--metric` | `-m` | string | `None` | Metric name to optimize |
| `--budget` | `-b` | int | `5` | Max number of experiments |
| `--agent` | `-a` | string | `claude` | Agent to use |
| `--direction`| `-d` | string | `minimize`| `minimize` or `maximize` |
| `--timeout` | `-t` | int | `600` | Timeout per experiment |
| `--dry-run` | | bool | `False` | Show plan without executing |

### dgov review-fix
Run the review-then-fix pipeline.

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--targets` | `-t` | string | `None` | File/directory paths to review |
| `--review-agent` | | string | `claude` | Agent for the review phase |
| `--fix-agent` | | string | `claude` | Agent for the fix phase |
| `--auto-approve` | | bool | `False` | Dispatch fixes immediately |
| `--severity` | | string | `medium` | Threshold: `critical`, `medium`, `low` |
| `--timeout` | | int | `600` | Timeout per phase |
