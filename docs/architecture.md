# Architecture

dgov is a Python-based CLI tool designed to orchestrate AI coding agents. This page explains the project's internal structure and core architectural principles.

## Module map

| Module | Purpose |
|--------|---------|
| `cli.py` | Entry point. Defines the Click command tree and flags. |
| `panes.py` | Pane lifecycle commands (create, wait, merge, close, etc.). Each module is imported directly — no barrel re-exports. |
| `persistence.py` | State management. SQLite database (WAL mode) and event journal logic. |
| `strategy.py` | Qwen 4B integration for task classification and slug generation. |
| `waiter.py` | Poll/wait logic for workers. Done and blocked detection. |
| `merger.py` | In-memory git merging, conflict resolution, and post-merge lint. |
| `batch.py` | DAG tier computation and parallel task runner. |
| `agents.py` | TOML-driven agent registry (built-in → user → project layers). Command builder for launching agents. |
| `backend.py` | `WorkerBackend` protocol for environmental abstraction. `TmuxBackend` implementation. |
| `tmux.py` | Low-level tmux command wrappers. |
| `openrouter.py` | OpenRouter API client for model queries. |
| `dashboard.py` | Live tmux dashboard with pane status display. |
| `experiment.py` | Sequential hypothesis testing loops and metric tracking. |
| `retry.py` | Auto-retry engine and retry policy logic. |
| `responder.py` | Auto-responder rules to unblock worker panes. |
| `review_fix.py` | Two-phase automated code review and fix pipeline. |
| `templates.py` | Prompt template substitution system. |
| `blame.py` | File-to-agent attribution using git log and event data. |
| `models.py` | Shared dataclasses (TaskSpec, MergeResult). |
| `preflight.py` | Pre-dispatch validation and auto-fix logic. |
| `state.py` | Status aggregation (panes, tunnel, kerberos). |

## Data flow

1. **CLI**: The `cli.py` entry point parses user input and dispatches a command.
2. **Routing**: Commands call functions in `panes.py`, which delegates to the appropriate specialized module.
3. **Persistence**: Every action is recorded in `persistence.py`. This writes a record to `state.db` (SQLite with WAL mode for lifecycle tracking) and an event to `events.jsonl` (append-only, for auditing).
4. **Backend**: Lifecycle operations (create, kill, capture) are handled by the `WorkerBackend` protocol in `backend.py`. The default `TmuxBackend` implementation translates these into tmux commands via `tmux.py`.

## Agent registry

The agent registry in `agents.py` is **TOML-driven** with a three-layer resolution system:

1. **Built-in** — Ship defaults for public CLIs (claude, codex, gemini, pi, etc.).
2. **User global** — `~/.dgov/agents.toml` for personal agent definitions and overrides.
3. **Project-local** — `<project_root>/.dgov/agents.toml` for repository-specific agents.

Each layer merges over the previous: new agent IDs are added, existing IDs get field-level overrides. Project-local config is a **security boundary** — it cannot define `health_check` or `health_fix` commands to prevent malicious repos from executing arbitrary shell code.

## WorkerBackend abstraction

To ensure dgov can adapt to different environments (e.g., Docker, SSH), all interaction with worker panes is done through the `WorkerBackend` protocol. This protocol defines 15+ methods for:
- Creating/destroying panes.
- Capturing output.
- Sending input/keys.
- Styling and logging.

## State machine enforcement

Every state change for a worker pane is validated against the `VALID_TRANSITIONS` table in `persistence.py`. This ensures that a pane cannot move, for example, from `merged` back to `active`, or from `closed` to `done`.

## Merge strategy

dgov uses an **in-memory plumbing merge** strategy. It calculates the merge tree using `git merge-tree` and creates a commit using `git commit-tree`. The main branch ref is only updated if the merge is clean. This is safer than a "porcelain" `git merge` because it never modifies your working tree unless the merge is successful.

## Security boundaries

- **Governor**: The governor is enforced to run only on the `main` branch to prevent accidental dispatches from inconsistent states.
- **Project Config**: Agents defined in project-local `agents.toml` cannot define `health_check` or `health_fix` commands. This prevents malicious repositories from executing arbitrary shell code on your machine.
- **Protected Files**: Files like `CLAUDE.md` are automatically restored from the base commit before merging to ensure a worker cannot clobber your project instructions.
