# Configuration files

dgov uses several files to manage state, configuration, and task context. This page provides a complete reference for the directory layout and file formats.

## Directory layout

All project-specific data lives in the `.dgov/` directory at your repository root.

```
.dgov/
├── state.db          # SQLite state database (WAL mode)
├── events.jsonl      # Append-only event journal
├── worktrees/        # Git worktrees for each worker
│   └── <slug>/
├── logs/             # Persistent stdout/stderr from workers
│   └── <slug>.log
├── prompts/          # Saved task prompts
│   └── <slug>--<ts>.txt
├── checkpoints/      # State snapshots
│   └── <name>.json
├── experiments/      # Experiment results and logs
│   ├── results/
│   └── <program>.jsonl
├── templates/        # Project-level prompt templates
│   └── <name>.toml
├── agents.toml       # Project-level agent overrides
├── hooks/            # Project-level lifecycle hooks
└── responses.toml    # Auto-responder rules
```

Global configuration lives in `~/.dgov/`.

```
~/.dgov/
├── agents.toml       # User-global agent overrides
├── hooks/            # Global lifecycle hooks
└── responses.toml    # Global auto-responder rules
```

## agents.toml

TOML format for defining or overriding agents (see [Agent registry](agent-registry.md) for full field reference). Files live at `~/.dgov/agents.toml` (global) or `.dgov/agents.toml` (project-level).

```toml
[agents.myagent]
name = "My Agent"
command = "agent-cli"
transport = "positional"
default_flags = "--verbose"
color = 45
max_concurrent = 2
health_check = "agent-cli --version"
health_fix = "brew upgrade agent-cli"
max_retries = 1
retry_escalate_to = "claude"

[agents.myagent.permissions]
acceptEdits = "--auto-accept"
bypassPermissions = "--yolo"

[agents.myagent.resume]
template = "agent-cli --continue{permissions}"

[agents.myagent.env]
MY_API_KEY = "sk-..."
```

**Required fields:** `command` (the CLI executable) and `transport` (`positional`, `option`, `stdin`, or `send-keys`).

**Priority:** built-in < user global (`~/.dgov/agents.toml`) < project local (`.dgov/agents.toml`). Each layer can override any field. For security, `health_check` and `health_fix` are ignored in project-level config.

## responses.toml

Format for auto-responder rules. These allow dgov to automatically reply to common prompts or escalate to the governor.

```toml
[[rules.rule]]
pattern = "(?i)do you want to proceed"
response = "yes"
action = "send"

[[rules.rule]]
pattern = "(?i)enter password"
action = "escalate"
```

**Actions:**
- `send`: types the `response` into the agent's stdin via tmux.
- `signal_done`: manually marks the pane as "done".
- `signal_failed`: manually marks the pane as "failed".
- `escalate`: emits a `pane_blocked` event to notify the governor.

## Prompt templates

TOML files at `.dgov/templates/<name>.toml` (see [Prompt templates](prompt-templates.md) for details).

## Protected files

These files are protected by dgov and are never carried forward from worker branches during a merge.

- `CLAUDE.md`, `CLAUDE.md.full`
- `THEORY.md`, `ARCH-NOTES.md`
- `.napkin.md`

## TDD status file

The environment variable `$DGOV_TDD_STATUS_FILE` provides a path where agents write structured JSON progress.

```json
{
  "step": 3,
  "step_name": "IMPLEMENT",
  "tests_passed": 4,
  "tests_failed": 2,
  "tests_total": 6,
  "elapsed_s": 45.2,
  "failing_tests": ["test_retry", "test_timeout"]
}
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `DGOV_ROOT` | Absolute path to the main repo root. |
| `DGOV_SLUG` | Unique identifier for the current task. |
| `DGOV_PROMPT` | The task prompt. |
| `DGOV_AGENT` | The ID of the agent running the task. |
| `DGOV_WORKTREE_PATH` | Path to the worker's worktree. |
| `DGOV_BRANCH` | Name of the worker's git branch. |
| `DGOV_TDD_STATUS_FILE`| Path for agents to write TDD progress. |
| `DGOV_SKIP_GOVERNOR_CHECK` | Set to `1` to bypass main-branch enforcement. |
