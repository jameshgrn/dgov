---
name: dgov
description: |
  Orchestrate AI coding agents via dgov. Dispatch workers to git worktrees,
  wait for completion, review diffs, merge results. Use when the user asks
  to "spin up a worker", "dispatch a pane", "run dgov", or delegates a task
  to an agent.
author: Jake Gearon
version: 1.0.0
date: 2026-03-13
---

# dgov — distributed governance

Orchestrate any AI coding agent across git worktrees. You are the governor.
You dispatch; you do not implement.

## Core loop

```bash
dgov pane create -a <agent> -p "<prompt>" -r <project_root>   # dispatch
dgov pane wait <slug>                                          # block until done
dgov pane review <slug>                                        # inspect diff
dgov pane merge <slug>                                         # integrate to main
dgov pane close <slug>                                         # cleanup
```

## Choosing an agent

```bash
dgov agents    # list all agents + install status
```

11 built-in agents. Pick based on the task:

| Agent | Best for |
|-------|----------|
| `claude` | Multi-file reasoning, architecture, ambiguous debugging |
| `codex` | Adversarial review, security audit, algorithmic work |
| `gemini` | Large context analysis, broad refactors |
| `pi` | Single-file mechanical tasks (with clear, numbered prompts) |
| `opencode` | General coding tasks |
| `cline` | TUI-based agent workflows |
| `qwen` | Alternative to pi for mechanical tasks |
| `amp` | Sourcegraph-powered coding |
| `cursor` | Cursor agent tasks |
| `copilot` | GitHub Copilot agent tasks |
| `crush` | Charmbracelet coding agent |

Default to the cheapest agent that can do the job. Escalate only on failure.

## Creating workers

### Simple dispatch
```bash
dgov pane create -a claude -p "Add retry logic to HTTP client" -r .
```

### With options
```bash
dgov pane create \
  -a pi \
  -p "Format all Python files with ruff" \
  -s format-py \              # custom slug (auto-generated otherwise)
  -m bypassPermissions \      # permission mode
  -r /path/to/project
```

### Prompting pi (Qwen 35B)
Pi needs explicit, numbered prompts. dgov auto-structures them, but for best
results provide:
1. Read steps first so pi sees the code
2. Exact code to add/change
3. Explicit lint + commit steps at the end

## Waiting and reviewing

```bash
dgov pane wait <slug>              # block until done
dgov pane wait <slug> -t 300       # 5-minute timeout
dgov pane wait-all                 # wait for all active panes

dgov pane review <slug>            # diff stat + verdict
dgov pane review <slug> --full     # include complete diff
dgov pane capture <slug> -n 50     # last 50 lines of pane output
dgov pane diff <slug>              # raw diff for inspection
dgov pane logs <slug>              # persistent log (survives pane death)
```

## Merging

```bash
dgov pane merge <slug>             # merge + close
dgov pane merge-all                # merge all done panes
```

After every merge, verify:
1. `uv run ruff check <changed_files>`
2. `uv run pytest <relevant_tests> -q -m unit`
3. `git rev-parse --abbrev-ref HEAD` returns `main`

## Recovery

```bash
dgov pane resume <slug>            # re-launch agent in existing worktree
dgov pane resume <slug> -a claude  # resume with a different agent
dgov pane retry <slug>             # fresh attempt with new worktree
dgov pane escalate <slug> -a claude  # re-dispatch to stronger agent
```

## Inspection

```bash
dgov status                        # full workstation health
dgov pane list                     # all panes with status
dgov blame <file> --all            # which agent/pane touched a file
dgov checkpoint create <name>      # snapshot current state
```

## Adding custom agents

Create `~/.dgov/agents.toml`:

```toml
[agents.my-agent]
command = "my-cli"
transport = "positional"           # or "option", "stdin", "send-keys"

# Optional
color = 42
max_concurrent = 2
health_check = "curl -sf http://localhost:8080/health"
health_fix = "start-my-server.sh"

[agents.my-agent.permissions]
bypassPermissions = "--yolo"

[agents.my-agent.env]
MY_API_KEY = "sk-..."
```

Transport types:
- **positional**: `tool "prompt"` (claude, codex, pi, cursor)
- **option**: `tool --flag "prompt"` (gemini, opencode, qwen, copilot)
- **stdin**: `echo "prompt" | tool` (amp)
- **send-keys**: tmux paste for TUI agents (cline, crush)

## Rules

- Never edit source files directly — dispatch workers
- Never checkout branches other than main
- Always review before merge
- Run lint + targeted tests after every merge
- Use `dgov pane wait` instead of manual polling
