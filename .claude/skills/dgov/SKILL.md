---
name: dgov
description: |
  Orchestrate AI coding agents via dgov. Dispatch workers to git worktrees,
  wait for completion, review diffs, merge results. Use when the user asks
  to "spin up a worker", "dispatch a pane", "run dgov", or delegates a task
  to an agent.
author: Jake Gearon
version: 2.0.0
date: 2026-03-17
---

# dgov — governor bootstrap

When this skill is invoked, perform the following steps IN ORDER before doing anything else. Report results as a compact status block.

## Step 1: Verify environment

Run these checks in parallel:

1. **tmux session**: `tmux list-sessions 2>&1 | grep dgov` — confirm a dgov session exists
2. **Branch**: `git rev-parse --abbrev-ref HEAD` — must be `main`
3. **Role**: `git rev-parse --git-dir` — must return `.git` (not a worktree)
4. **Active panes**: `dgov status -r .` — show current worker state

## Step 2: Check agent availability

Run in parallel:

1. **pi health**: `curl -sf http://localhost:11434/api/tags --max-time 3` or equivalent pi health endpoint — if unreachable, note "pi unavailable (River tunnel down?)"
2. **GPU status** (if pi healthy): `ssh river "nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader" 2>/dev/null` — show GPU load

## Step 3: Report readiness

Print a compact status block like:

```
dgov governor ready
  session: dgov-dgov (attached)
  branch:  main
  panes:   0 active / 0 done / 0 failed
  pi:      healthy (GPU 0: 0%, GPU 1: 0%)
```

Or if something is wrong:

```
dgov governor NOT READY
  session: none found — run `dgov` to create one
  branch:  feature-x — switch to main first
  pi:      unreachable — check River tunnel
```

## Step 4: Enter governor mode

After reporting status, you are the governor. Remind yourself:

- You dispatch workers via `dgov pane create`. You do not edit `src/` or `tests/` directly.
- Default agent: `pi`. Escalate to `claude`, `codex`, `gemini` only when pi can't do the job.
- Always `review` before `merge`. Run lint + targeted tests after merge.
- One action per turn. Use the action grammar from CLAUDE.md.

Then ask: **"What are we working on?"**

## Reference: core commands

```bash
dgov pane create -a <agent> -p "<prompt>" -r .   # dispatch
dgov pane list                                     # status
dgov pane wait <slug>                              # block until done
dgov pane review <slug>                            # inspect diff
dgov pane merge <slug>                             # integrate
dgov pane land <slug>                              # review+merge+close
dgov pane close <slug>                             # cleanup
dgov dashboard --pane                              # launch dashboard
```

## Reference: agent selection

| Agent | When to use |
|-------|-------------|
| `pi` | Default. Single-file, well-scoped, numbered-step prompts |
| `claude` | Multi-file reasoning, architecture, ambiguous debugging |
| `codex` | Adversarial review, security audit, algorithms |
| `gemini` | Large context, broad refactors |
| `hunter` | Like pi but via OpenRouter (free, 1M context) |
