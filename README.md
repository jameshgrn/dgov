# dgov

A meta harness for AI coding agents.

A test harness runs tests. A meta harness runs the things that write the code. dgov sits above any CLI-based coding agent — Claude Code, Codex, Gemini, Cursor, Copilot, Cline, and others — and manages what they cannot manage about themselves: isolation, lifecycle, and integration.

The problem is simple. AI coding agents edit files. When two agents edit the same repo at the same time, they collide. When an agent runs unsupervised, it stalls at permission prompts, drifts off-task, or silently fails. When it finishes, its changes sit on a branch that nobody reviews. dgov solves each of these problems through one mechanism: git worktrees governed by a uniform lifecycle.

Each agent gets its own worktree. Each worktree gets its own branch. The governor — you, sitting on `main` — dispatches tasks, waits for completion, reviews diffs, and merges results. The agents write code. dgov tracks state, logs events, and attributes every change to the agent that made it.

The harness is agent-agnostic (11 agents built in, any CLI tool added via TOML), backend-agnostic (tmux today, Docker and SSH tomorrow via a `WorkerBackend` protocol), and workflow-agnostic. Single tasks, batch DAGs, experiment loops, and review-fix pipelines all compose from four primitives: dispatch, wait, review, merge.

## Design

- **Lightweight** — pure Python, one dependency (click), no daemon, no server
- **Extensible** — add agents via TOML config, backends via protocol, hooks via shell scripts
- **Developer-friendly** — git worktrees, tmux panes, CLI commands; no new paradigm to learn
- **Composable** — batch mode, experiment loops, and review-fix pipelines compose from the same primitives
- **Opinionated where it matters** — governor stays on `main`, workers get worktrees, protected files are restored before merge

## Install

```bash
uv tool install dgov
```

Requires: Python 3.12+, git, tmux.

## Quick start

```bash
dgov pane create -a claude -p "Add retry logic to the HTTP client"
dgov pane wait <slug>
dgov pane review <slug>
dgov pane merge <slug>
```

State lives in `.dgov/state.db` (SQLite). Events append to `.dgov/events.jsonl`.

## Commands

### Core

| Command | Description |
|---------|-------------|
| `dgov status` | Show session state and pane health |
| `dgov agents` | List all registered agents and install status |
| `dgov dashboard` | Live TUI showing pane status, events, and metrics |

### Pane lifecycle

| Command | Description |
|---------|-------------|
| `dgov pane create` | Create a new worker pane (worktree + tmux + agent) |
| `dgov pane list` | List all panes |
| `dgov pane wait` | Block until a pane finishes |
| `dgov pane review` | Inspect a pane's diff and verdict |
| `dgov pane merge` | Merge a pane's branch into main |
| `dgov pane close` | Close a pane and clean up worktree |
| `dgov pane resume` | Re-launch agent in existing worktree |
| `dgov pane retry` | Fresh attempt with new worktree |
| `dgov pane escalate` | Re-dispatch to a stronger agent |
| `dgov pane capture` | Capture recent pane output |
| `dgov pane logs` | Persistent log (survives pane death) |
| `dgov pane diff` | Raw diff for inspection |
| `dgov pane lazygit` | Launch lazygit in a utility pane |
| `dgov pane top` | Launch btop in a utility pane |

### Automation

| Command | Description |
|---------|-------------|
| `dgov batch <spec>` | Execute a batch spec with DAG-ordered parallelism |
| `dgov experiment` | Run iterative experiments with accept/reject loop |
| `dgov review-fix` | Review-then-fix pipeline with severity filtering |

### Tools

| Command | Description |
|---------|-------------|
| `dgov blame <file>` | Show which agent/pane last touched a file (`--line-level` for per-line) |
| `dgov openrouter models` | List available free models on OpenRouter |
| `dgov openrouter status` | Show API key status, default model, connectivity |
| `dgov openrouter test` | Send a test prompt via OpenRouter |
| `dgov template list` | List all prompt templates (built-in + user) |
| `dgov template create` | Create a new template in `.dgov/templates/` |
| `dgov template show` | Show template details and required variables |
| `dgov checkpoint create` | Create a named checkpoint of current state |
| `dgov checkpoint list` | List all checkpoints |

## Built-in agents

| Agent | CLI | Install check |
|-------|-----|---------------|
| `claude` | Claude Code | `claude` |
| `codex` | Codex | `codex` |
| `gemini` | Gemini CLI | `gemini` |
| `cursor` | Cursor CLI | `cursor` |
| `opencode` | OpenCode | `opencode` |
| `cline` | Cline CLI | `cline` |
| `amp` | Amp CLI | `amp` |
| `copilot` | Copilot CLI | `copilot` |
| `crush` | Crush CLI | `crush` |

User agents: `~/.dgov/agents.toml` (global) or `.dgov/agents.toml` (per-project). See `dgov agents` for what's installed.

## Configuration

- `.dgov/agents.toml` — custom agent definitions
- `.dgov/templates/` — prompt templates
- `.dgov/batch/` — batch spec files
- `.dgov/state.db` — SQLite state (auto-created)
- `.dgov/events.jsonl` — event log

## License

MIT
