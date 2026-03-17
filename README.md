# dgov

A meta harness for AI coding agents.

A test harness runs tests. A meta harness runs the things that write the code. dgov sits above any CLI-based coding agent — Claude Code, Codex, Gemini, Cursor, Copilot, Cline, and others — and manages what they cannot manage about themselves: isolation, lifecycle, and integration.

The problem is simple. AI coding agents edit files. When two agents edit the same repo at the same time, they collide. When an agent runs unsupervised, it stalls at permission prompts, drifts off-task, or silently fails. When it finishes, its changes sit on a branch that nobody reviews. dgov solves each of these problems through one mechanism: git worktrees governed by a uniform lifecycle.
Each agent gets its own worktree. Each worktree gets its own branch. The governor — you, sitting on `main` — dispatches tasks, waits for completion, reviews diffs, and merges results. The agents write code. dgov tracks state, logs events, and attributes every change to the agent that made it.

## Lifecycle

Panes follow a strict state machine enforced by the persistence layer. Transitions are validated to ensure consistency across the worker lifecycle.

```mermaid
stateDiagram-v2
    [*] --> active: dgov pane create
    active --> done: task finished (commits or done-file)
    active --> failed: agent crashed or exit-code file
    active --> abandoned: tmux pane dead / no output
    active --> escalated: dgov pane escalate
    active --> timed_out: wait timeout reached
    active --> closed: dgov pane close
    active --> superseded: retried with fresh attempt

    done --> reviewed_pass: dgov pane review (pass)
    done --> reviewed_fail: dgov pane review (fail)
    done --> merged: dgov pane merge
    done --> merge_conflict: git merge failed

    reviewed_pass --> merged: dgov pane merge
    reviewed_pass --> merge_conflict: git merge failed

    merge_conflict --> merged: manual fix + merge
    merge_conflict --> escalated: re-dispatch fix task

    failed --> escalated: dgov pane escalate
    failed --> closed: dgov pane close

    timed_out --> done: late finish detected
    timed_out --> escalated: re-dispatch to stronger agent

    merged --> closed: cleanup (automatic)
    escalated --> closed: cleanup
    superseded --> closed: cleanup
    abandoned --> closed: cleanup

    closed --> [*]
```

## Signal Flow

The Governor and Workers communicate through three primary channels:

1.  **State DB (SQLite):** Authoritative state (active, done, merged) and event journal.
2.  **Filesystem (done signals):** Workers touch `.dgov/done/<slug>` on success or `.dgov/done/<slug>.exit` on failure. These are authoritative signals that override background detection.
3.  **Tmux/Pseudo-terminal:** The governor captures worker output for stabilization detection and can send keystrokes/responses back to the agent via `dgov pane respond`.

Done detection uses a prioritized fallback strategy:
- **Authoritative:** Presence of a `.done` or `.done.exit` file.
- **Inferred:** Git commits on the worker branch (30s grace period).
- **Stabilization:** No output for N seconds (TUI agents).
- **Liveness:** Tmux pane is dead or process is gone.

## Yapper Interface

The Yapper is a conversational front-end that classifies natural language into actionable dgov tasks.

```bash
dgov yap "Fix the overflow bug in the footer using claude"
```

It classifies input into:
- **COMMAND:** Dispatches a worker immediately.
- **IDEA:** Noted in the event log for later.
- **QUESTION:** Answers from the codebase or state DB.
- **CHATTER:** Acknowledged without side effects.

---

## Design

- **Lightweight** — pure Python, one dependency (click), no daemon, no server
- **Extensible** — add agents via TOML config, backends via protocol, hooks via shell scripts
- **Developer-friendly** — git worktrees, tmux panes, CLI commands; no new paradigm to learn
- **Composable** — DAGs, missions, and batch specs compose from the same primitives
- **Opinionated where it matters** — governor stays on `main`, workers get worktrees, protected files are restored before merge

## Install

```bash
uv tool install dgov
```

Requires: Python 3.12+, git, tmux.

## Quick start

Run `dgov` with no arguments to launch the governor workspace:

```bash
dgov                          # launches dashboard + lazygit in tmux
dgov --governor gemini        # override governor agent
```

Or dispatch a worker directly:

```bash
dgov pane create -a claude -p "Add retry logic to the HTTP client"
dgov pane wait <slug>
dgov pane review <slug>
dgov pane merge <slug>
```

Or do it in one step:

```bash
dgov pane create -a claude -p "Add retry logic to the HTTP client"
dgov pane land <slug>          # review + merge + close
```

State and events live in `.dgov/state.db` (SQLite, WAL mode).

## Commands

### Core

| Command | Description |
|---------|-------------|
| `dgov status` | Show session state and pane health |
| `dgov agents` | List all registered agents and install status |
| `dgov dashboard` | Live TUI showing pane status, events, and metrics |
| `dgov dashboard --pane` | Launch dashboard in a tmux split pane |

### Pane lifecycle

| Command | Description |
|---------|-------------|
| `dgov pane create` | Create a worker pane (worktree + tmux + agent) |
| `dgov pane util` | Run a command in a utility pane (no worktree) |
| `dgov pane list` | List all panes with state, agent, duration |
| `dgov pane wait` | Block until one or more panes finish |
| `dgov pane wait-all` | Block until all active panes finish |
| `dgov pane review` | Inspect a pane's diff, commit count, and verdict |
| `dgov pane merge` | Merge a pane's branch into main (squash by default, `--no-squash` or `--rebase`) |
| `dgov pane land` | Review + merge + close in one step |
| `dgov pane merge-all` | Merge all reviewed-pass panes |
| `dgov pane close` | Close a pane and clean up worktree (idempotent) |
| `dgov pane resume` | Re-launch agent in existing worktree |
| `dgov pane retry` | Fresh attempt with new worktree |
| `dgov pane retry-or-escalate` | Retry with auto-escalation policy |
| `dgov pane escalate` | Re-dispatch to a stronger agent |
| `dgov pane classify` | Recommend an agent for a task |
| `dgov pane output` | Clean ANSI-stripped log text |
| `dgov pane capture` | Live tmux pane capture |
| `dgov pane logs` | Raw persistent log (survives pane death) |
| `dgov pane diff` | Raw diff for inspection |
| `dgov pane message` | Send text to a running worker |
| `dgov pane respond` | Reply to an agent's prompt |
| `dgov pane nudge` | Prod a stalled agent |
| `dgov pane signal` | Manually signal a pane as done or failed |
| `dgov pane prune` | Clean up stale pane records |
| `dgov pane merge-request` | Enqueue a merge (used by LT-GOVs) |

### DAG runner

Run multi-task workflows defined in TOML. Tasks declare dependencies and file touches; dgov computes execution tiers via topological sort with file-overlap detection.

```bash
dgov dag run TASKS.toml                    # execute all tiers
dgov dag run TASKS.toml --dry-run          # show tier plan without executing
dgov dag run TASKS.toml --tier 0           # execute only tier 0
dgov dag run TASKS.toml --skip slow-task   # skip a task and its dependents
dgov dag run TASKS.toml --no-auto-merge    # hold merges for manual review
dgov dag merge TASKS.toml                  # merge held tasks from a prior run
```

Features: retry with augmented prompts on failure, agent escalation chains, per-agent concurrency limits, crash-safe resume via SQLite, `commit_message` from TOML used as merge commit message.

### Orchestration

| Command | Description |
|---------|-------------|
| `dgov mission` | Single-prompt orchestration: dispatch, wait, review, merge |
| `dgov batch` | Execute a batch spec with DAG-ordered parallelism |
| `dgov experiment` | Iterative experiments with accept/reject loop |
| `dgov review-fix` | Review-then-fix pipeline with severity filtering |
| `dgov yap` | Classify agent output (actionable vs chatter) |

### LT-GOV (delegation)

A lieutenant governor is a claude worker with a meta-prompt that itself dispatches workers. The governor delegates a broad task to an LT-GOV, which breaks it down, dispatches sub-workers, and submits merge requests back to the governor's queue.

```bash
dgov pane create -a claude -T lt-gov -V task_list="..." # dispatch LT-GOV
dgov merge-queue list                                    # see pending merges
dgov merge-queue process                                 # claim and execute
```

| Command | Description |
|---------|-------------|
| `dgov pane merge-request` | Enqueue a merge (used by LT-GOVs) |
| `dgov merge-queue list` | Show pending merge requests |
| `dgov merge-queue process` | Claim and execute next merge |

### Tools

| Command | Description |
|---------|-------------|
| `dgov blame <file>` | Show which agent/pane last touched a file |
| `dgov openrouter models` | List available models on OpenRouter |
| `dgov openrouter status` | Show API key status, default model, connectivity |
| `dgov openrouter test` | Send a test prompt via OpenRouter |
| `dgov template list` | List all prompt templates |
| `dgov template create` | Create a new template |
| `dgov template show` | Show template details and required variables |
| `dgov checkpoint create` | Create a named checkpoint |
| `dgov checkpoint list` | List all checkpoints |

## Built-in agents

| Agent | CLI | Done detection |
|-------|-----|----------------|
| `claude` | Claude Code | commit (30s grace after last commit) |
| `codex` | Codex CLI | exit |
| `gemini` | Gemini CLI | exit |
| `cursor` | Cursor CLI | stable |
| `opencode` | OpenCode | exit |
| `cline` | Cline CLI | stable |
| `qwen` | Qwen CLI | exit |
| `amp` | Amp CLI | exit |
| `pi` | pi CLI | exit |
| `copilot` | Copilot CLI | exit |
| `crush` | Crush CLI | stable |

User agents: `~/.dgov/agents.toml` (global) or `.dgov/agents.toml` (per-project). See `dgov agents` for what's installed.

Done strategies: `exit` (process exits), `commit` (watches for git commits), `stable` (output stabilization), `signal` (done file touched).

## Hooks

Shell scripts that run at lifecycle events. Three levels of precedence:

1. `.dgov/hooks/` — per-repo (highest priority)
2. `.dgov-hooks/` — team/shared (checked into repo)
3. `~/.dgov/hooks/` — global (lowest priority)

| Hook | When |
|------|------|
| `worktree_created` | After worktree + branch are set up, before agent launches |
| `pre_merge` | Before merging a worker's branch (restore protected files) |
| `post_merge` | After merge (lint changed files, verify protected files) |
| `before_worktree_remove` | Before deleting a worktree (archive artifacts) |

## Configuration

- `.dgov/config.toml` — per-repo settings (`governor_agent`, `governor_permissions`)
- `.dgov/agents.toml` — custom agent definitions (commands, env, done strategy)
- `.dgov/templates/` — prompt templates with variable substitution
- `.dgov/state.db` — SQLite state and events (auto-created, WAL mode)
- `~/.dgov/config.toml` — global settings (OpenRouter API key, defaults)
- `~/.dgov/agents.toml` — global custom agents

## License

MIT
