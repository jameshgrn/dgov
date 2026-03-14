# Core concepts

dgov is an orchestrator that manages the lifecycle of AI workers. It's built on a few fundamental concepts.

## Governor vs Workers

- **Governor**: The process that runs on your `main` repository branch. It dispatches tasks, waits for results, and merges them. It never writes code directly.
- **Workers**: Short-lived processes that run in isolated git worktrees. Each worker executes an agent's command with a specific prompt, commits its changes, and exits.

## Git Worktrees

Isolation is achieved via `git worktree`. Each worker gets its own worktree directory at `.dgov/worktrees/<slug>/`. This ensures that multiple agents can work in parallel without file conflicts or shared state. Every worker runs on its own branch named after its **slug**.

## Panes

A **pane** is the fundamental unit of work in dgov. It represents the combination of:
1. A git worktree.
2. A unique slug.
3. A backend process (e.g., a tmux pane).
4. An agent CLI process.

## Slugs

Slugs are unique identifiers for each task (e.g., `fix-off-by-one`). They are auto-generated from your prompt using a local Qwen 4B model (if available) or can be manually specified with the `-s` flag.

## Backends

`WorkerBackend` is a protocol that allows swapping the execution environment.
- **TmuxBackend (default)**: Manages local tmux panes and windows.
- **Docker/SSH**: Future backends allow workers to run in containers or on remote servers.

## Agents

An agent is any CLI tool that can accept a prompt and produce code. dgov provides a unified interface for 11+ agents, handling the transport of prompts via positional arguments, CLI options, `send-keys`, or `stdin`.

## State machine

Every pane follows a strict state machine defined in `persistence.py`.

```
active --→ done --→ reviewed_pass --→ merged --→ closed
   |        |           |                ↑
   |        |           +--→ merge_conflict
   |        |
   +--→ failed --→ escalated --→ superseded
   |
   +--→ timed_out
   |
   +--→ abandoned
```

**Common States:**
- `active`: The agent is currently running.
- `done`: The agent has finished (either signaled "done" or committed changes).
- `merged`: The worker branch has been merged into `main`.
- `closed`: The worktree and tmux pane have been removed.

## Protected files

dgov enforces a strict boundary for specific files (e.g., `CLAUDE.md`, `.napkin.md`). These files are **never** carried forward from worker branches during a merge. dgov automatically restores the version from the base branch before completing the merge.

## Main-branch enforcement

By default, the governor refuses to run unless it is on the `main` branch of the root repository. This ensures all dispatches and merges are coordinated from a clean, central source. Set `DGOV_SKIP_GOVERNOR_CHECK=1` to override this for development.
