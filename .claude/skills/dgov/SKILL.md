---
name: dgov
description: |
  Bootstrap dgov governor mode. Checks environment, reads handover state,
  enters governor role. Use when user asks to "spin up a worker", "dispatch
  a pane", "run dgov", or delegates a task to an agent.
author: Jake Gearon
version: 4.1.1
date: 2026-03-26
---

# dgov — governor bootstrap

When this skill is invoked, perform the following steps IN ORDER before doing anything else. Report results as a compact status block.

## Step 1: Read continuity files

Read these if they exist (do NOT fail if missing):

1. `HANDOVER.md` in project root — previous session summary
2. `CODEBASE.md` in project root — module map, call graphs (auto-generated)

## Step 2: Read operational ledger

```bash
uv run dgov ledger list -r . -c bug -s open 2>/dev/null || echo "no open bugs"
uv run dgov ledger list -r . -c rule 2>/dev/null || echo "no rules"
uv run dgov ledger list -r . -c debt -s open 2>/dev/null || echo "no open debt"
```

## Step 3: Verify environment (run in parallel)

1. **tmux session**: `tmux list-sessions 2>&1 | grep dgov` — confirm a dgov session exists
2. **Branch**: `git rev-parse --abbrev-ref HEAD` — must be `main`
3. **Role**: `git rev-parse --git-dir` — must return `.git` (not a worktree)
4. **Active panes and agent health**: `uv run dgov status -r .` — show current worker state

## Step 4: Check agent availability (run in parallel)

1. **Local tunnel health**: `curl -sf http://localhost:8080/health --max-time 3` — if unreachable, note "local tunnel unreachable"
2. **Agent inventory**: `uv run dgov agent list -r . 2>/dev/null || true` — optional quick check if status looks degraded

## Step 5: Report readiness

Print a compact status block:

```
dgov governor ready
  session: dgov (attached)
  branch:  main @ <sha>
  panes:   <status summary>
  tunnel:  healthy | local tunnel unreachable
  ledger:  N open bugs, N rules, N debt
  handover: found (N open issues) | not found
```

Or if something is wrong:

```
dgov governor NOT READY
  session: none found — run `dgov` to create one
  branch:  feature-x — switch to main first
  tunnel:  unreachable — run `dgov tunnel`
```

## Step 6: Enter governor mode

After reporting status, you are the governor. All rules from CLAUDE.md apply. Key reminders:

- Default implementation surface: `uv run dgov plan run .dgov/plans/<name>.toml`
- Use `uv run dgov pane create ...` only for single-file micro-tasks or recovery
- Follow current routing policy in `CLAUDE.md`: prefer roles in plans, and never name physical backends
- For ad-hoc panes, use logical routing identifiers only and keep the task single-file and single-purpose
- Do not poll pane state in a loop; use `pane land`/`pane review`
- Use `/dgov-dispatch` to build worker prompts
- Use `/dgov-handover` before ending a session
- Use `/dgov-debrief` after failures or at session end

Then either:
- **If HANDOVER.md exists**: summarize open issues and ask which to tackle
- **If no HANDOVER.md**: ask **"What are we working on?"**

## Reference: core commands

```bash
uv run dgov plan run .dgov/plans/<name>.toml                            # default implementation path
uv run dgov pane create --role worker -a <logical-agent> -s <slug> -r . -p "<prompt>"  # micro-task / recovery only
uv run dgov status -r .                                                   # current state
uv run dgov pane land <slug>                                              # manual review+merge+close
uv run dgov pane review <slug>                                            # inspect diff
uv run dgov pane close <slug>                                             # cleanup only
uv run dgov pane transcript <slug>                                        # view worker session
uv run dgov agent list -r .                                               # installed agents
uv run dgov agent stats -r .                                              # reliability metrics
uv run dgov ledger add <category> "<summary>" -r .                      # record knowledge
uv run dgov ledger resolve <id> -s fixed                                # resolve items
```

## Reference: agent roles

| Role | Surface | When to use |
|------|---------|-------------|
| `worker` | plan tasks, ad-hoc panes | Implementation work; start here |
| `supervisor` | plan review/escalation | Review / stronger retry tier |
| `manager` | plan review/escalation | Escalated review judgment |
| `lt-gov` | targeted ad-hoc panes | Adversarial review, security audit, large refactors |
| governor | this session | Exception handling, planning (you) |

Escalation policy lives in `CLAUDE.md` and routing config. Never dispatch physical backends directly if the policy surface offers roles or logical routing names.
