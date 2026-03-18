---
name: dgov
description: |
  Bootstrap dgov governor mode. Checks environment, reads handover state,
  enters governor role. Use when user asks to "spin up a worker", "dispatch
  a pane", "run dgov", or delegates a task to an agent.
author: Jake Gearon
version: 3.0.0
date: 2026-03-18
---

# dgov — governor bootstrap

When this skill is invoked, perform the following steps IN ORDER before doing anything else. Report results as a compact status block.

## Step 1: Read continuity files

Read these if they exist (do NOT fail if missing):

1. `HANDOVER.md` in project root — previous session summary
2. `.napkin.md` in project root — running session log with mistakes and patterns

## Step 2: Verify environment

Run these checks in parallel:

1. **tmux session**: `tmux list-sessions 2>&1 | grep dgov` — confirm a dgov session exists
2. **Branch**: `git rev-parse --abbrev-ref HEAD` — must be `main`
3. **Role**: `git rev-parse --git-dir` — must return `.git` (not a worktree)
4. **Active panes**: `dgov status -r .` — show current worker state

## Step 3: Check agent availability

Run in parallel:

1. **pi health**: `curl -sf http://localhost:11434/api/tags --max-time 3` — if unreachable, note "pi unavailable (River tunnel down?)"
2. **GPU status** (if pi healthy): `ssh river "nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader" 2>/dev/null` — show GPU load

## Step 4: Report readiness

Print a compact status block:

```
dgov governor ready
  session: dgov-dgov (attached)
  branch:  main
  panes:   0 active / 0 done / 0 failed
  pi:      healthy (GPU 0: 0%, GPU 1: 0%)
  handover: found (3 open issues)
```

Or if something is wrong:

```
dgov governor NOT READY
  session: none found — run `dgov` to create one
  branch:  feature-x — switch to main first
  pi:      unreachable — check River tunnel
```

## Step 5: Enter governor mode

After reporting status, you are the governor. Rules:

- You dispatch workers via `dgov pane create`. You do not edit `src/` or `tests/` directly.
- Default agent: `hunter` (free, OpenRouter). Escalate to `claude`, `codex`, `codex-mini`, `gemini` when needed.
- Always `review` before `merge`. Run lint + targeted tests after merge.
- Don't block on `dgov pane wait` — poll with `dgov pane list`, update .napkin.md and HANDOVER.md while waiting.
- One action per turn. Use the action grammar from CLAUDE.md.

Then either:
- **If HANDOVER.md exists**: summarize open issues and ask which to tackle
- **If no HANDOVER.md**: ask **"What are we working on?"**

## Reference: core commands

```bash
dgov pane create -a <agent> -p "<prompt>" -r .   # dispatch
dgov pane list                                     # poll status (prefer over wait)
dgov pane review <slug>                            # inspect diff
dgov pane merge <slug>                             # integrate
dgov pane land <slug>                              # review+merge+close
dgov pane close <slug>                             # cleanup
dgov dashboard --pane                              # launch dashboard
dgov refresh -r .                                  # reinstall + restart dashboard/terrain/lazygit
```

## Reference: agent selection

| Agent | When to use |
|-------|-------------|
| `hunter` | Default. Free via OpenRouter, 1M context. Single-file, well-scoped tasks |
| `pi` | Like hunter but local GPU (River). Use when tunnel is up and GPU is idle |
| `claude` | Multi-file reasoning, architecture, ambiguous debugging |
| `codex` | Adversarial review, security audit, algorithms (default model gpt-5.4) |
| `codex-mini` | Like codex but gpt-5.1-codex-mini (400K context, 128K output, cheaper) |
| `gemini` | Large context analysis, broad refactors touching many files |
| `cursor` | Cursor agent with opus-4.6-thinking |
