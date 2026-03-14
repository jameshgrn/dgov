# dgov

A meta harness for AI coding agents.

dgov is not an AI coding agent. It's the layer that sits above them. It orchestrates any CLI-based agent — Claude Code, Codex, Gemini, Cursor, Copilot, Cline, and others — through a uniform lifecycle: dispatch a task, wait for completion, review the diff, merge the result. The agents do the coding. dgov manages the workflow.

## Why "meta harness"?

A test harness runs tests. A meta harness runs the things that write the code.

Each agent gets its own git worktree, so multiple agents can work on the same repo simultaneously without stepping on each other. The governor (you, on `main`) dispatches workers into isolated branches, reviews their output, and merges results back. dgov tracks state, logs events, and attributes changes to the agent that made them.

The harness is agent-agnostic (11 agents built in, any CLI tool added via TOML), backend-agnostic (tmux today, Docker/SSH tomorrow via a `WorkerBackend` protocol), and workflow-agnostic (single tasks, batch DAGs, experiment loops, review-fix pipelines — all built on the same dispatch-wait-review-merge primitives).

## Design

- **Lightweight** — pure Python, one dependency (click), no daemon, no server
- **Extensible** — add agents via TOML config, backends via protocol, hooks via shell scripts
- **Developer-friendly** — git worktrees, tmux panes, CLI commands; no new paradigm to learn
- **Composable** — batch mode, experiment loops, and review-fix pipelines compose from the same primitives
- **Opinionated where it matters** — governor stays on `main`, workers get worktrees, protected files are restored before merge

## Install

```
uv tool install -e /path/to/dgov
```

Requires Python >= 3.12 and a running `tmux` session.

## Quick start

```bash
dgov pane create -a claude -p "Add retry logic to the HTTP client"
dgov pane wait <slug>
dgov pane review <slug>
dgov pane merge <slug>
```

**[Documentation](https://sandfrom.space/dgov/)** — installation, guides, CLI reference, architecture.

## Agent registry

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

```
dgov agents          # list agents + installed status
```
