# dgov

A meta harness for AI coding agents.

dgov is not an AI coding agent. It's the layer that sits above them. It orchestrates any CLI-based agent — Claude Code, Codex, Gemini, Cursor, Copilot, Cline, and others — through a uniform lifecycle: dispatch a task, wait for completion, review the diff, merge the result. The agents do the coding. dgov manages the workflow.

## Install

```bash
uv tool install dgov
```

Requires: Python 3.12+, git, tmux.

## How it works

dgov dispatches tasks to AI coding agents running in isolated git worktrees. Each worker gets its own branch, tmux pane, and agent CLI. A governor (usually Claude or pi) dispatches, reviews, and merges work.

State is stored in `.dgov/state.db` (SQLite WAL). Events are logged to `.dgov/events.jsonl`.

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
| `dgov pane preflight` | Run pre-dispatch checks |
| `dgov pane edit` | Edit a pane's prompt while running |
| `dgov pane top` | Launch btop in a utility pane |
| `dgov pane k9s` | Launch k9s in a utility pane |

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
