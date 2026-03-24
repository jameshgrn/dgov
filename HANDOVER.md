# HANDOVER — 2026-03-24 (Eval-First Planning Pipeline Live)

## Current State

Eval-first planning system is **live and tested end-to-end**.
Full pipeline works: plan → validate → compile → DAG submit → monitor dispatch → worker execute → review → merge → eval evidence → governor notified.
`dgov plan run --wait` blocks on per-process pipe, reports eval PASS/FAIL, exits 0/1.
Selftest plan validates: commit landed, zero ghost panes, lint clean.
Dashboard shows workers with live millisecond duration ticking.
1598 tests passing. All pushed to origin/main.

## Completed

### Eval Contract Surfaces
- Typed `dag_evals` + `dag_unit_eval_links` tables persisted on plan submission
- `dag status` CLI shows per-eval PASS/FAIL/... markers derived from unit merge status + evidence results
- Review artifacts include eval context; model review prompt includes eval statements
- Dashboard header shows `E:0/2` → `E:2/2` progress

### Eval Evidence Execution
- Monitor runs evidence commands on DAG completion, records in `dag_eval_results` table
- `evals_verified` is the single terminal event (emitted on both completion and failure)
- `dgov plan run --wait` wakes on `evals_verified` only — no timing dance
- `dgov plan verify <run_id>` for manual re-run

### Per-Process Notify Pipes
- `.dgov/notify/<pid>.pipe` — all readers wake independently
- Only deletes pipes for dead processes (PID check), not between-reads
- Replaced shared single FIFO that only delivered to one reader

### Ghost Pane Elimination
- Inline DAG pane cleanup in `_drive_dag` after `DagDone` (not deferred to event loop)
- Validate worktree with `git rev-parse` before preserving on close failure
- All terminal states (superseded, timed_out, abandoned) auto-force close
- 60s grace period in orphan pruner prevents worktree creation race

### Dashboard Fixes
- SQLite `isolation_level=None` so cached connection sees cross-process writes
- Live duration from `created_at` at render time (4fps), millisecond format
- 5s data refresh ceiling, 30s done notification expiry
- `created_at` passed through `list_worker_panes` result dict

### Worker Context & Delivery
- CODEBASE.md injected directly into worker/lt-gov instructions (not a hint)
- Auto-refreshed on dispatch if stale (older than HEAD commit)
- LLM-native CODEBASE.md format: 22% fewer tokens
- Pi stdin transport (positional hangs in v0.62.0), `--model` flag required
- Contradiction detection: `worker_contradiction` event on hallucinated "already done"

### Infrastructure Fixes
- `select()` → `poll()` (no FD_SETSIZE limit)
- `_ensure_notify_pipe` TOCTOU race fixed
- 200ms settle in tmux `send_command` for ssource doubling
- Global CLAUDE.md cleaned: stale dgov section → pointer, commit conventions repo-specific
- Governor session-start checklist in repo CLAUDE.md

## Key Decisions
- `evals_verified` as single terminal event — simplifies `--wait` to one event type
- Per-process pipes over shared FIFO — scales to any number of readers
- Inline cleanup over event-loop deferred — event loop can't re-process its own emitted events
- `isolation_level=None` — Python's default deferred transactions block cross-process WAL reads
- Governor exception for this session — critical bootstrapping, not normal workflow

## Open Issues
- **Monitor doesn't always wake on first DAG submission**: needs restart if started before submission. Bootstrap picks up active runs but pipe notification from `plan run` sometimes misses.
- **Dual monitor processes**: `ensure_monitor_running` can race and spawn duplicates. Need flock singleton.
- **`_ALLOWED_EVAL_KINDS` bloated**: 18+ kinds from repeated e2e test runs. Trim to useful set.
- **OpenRouter credits exhausted**: `qwen35-*` fallback agents fail. Top up or remove from routing.
- **Worker hallucination**: 9B models sometimes narrate instead of using tools. Stronger instructions added but not fully validated.

## Next Steps
1. **Governor auto-planning**: wire `serialize_plan()` so governor constructs PlanSpec programmatically
2. **`dgov selftest` CLI command**: wrap selftest plan as built-in for CI
3. **Monitor singleton**: flock-based guard, one monitor per session
4. **Trim eval kinds**: remove e2e test debris
5. **Monitor wakeup reliability**: ensure fresh monitor picks up DAG runs submitted before it started

## Important Files
- `src/dgov/plan.py` — PlanSpec, validate, compile, run_plan, verify_eval_evidence
- `src/dgov/persistence.py` — per-process notify pipes, dag_eval_results, autocommit SQLite
- `src/dgov/monitor.py` — _drive_dag (inline cleanup + eval evidence), evals_verified emission
- `src/dgov/dashboard.py` — live duration, eval header, 5s refresh, 30s done expiry
- `src/dgov/lifecycle.py` — CODEBASE.md injection, ghost pane fix, contradiction detection
- `src/dgov/executor.py` — eval context in review path
- `src/dgov/cli/plan_cmd.py` — `plan run --wait`, `plan verify`
- `src/dgov/cli/dag_cmd.py` — eval display in `dag status`
- `src/dgov/status.py` — worktree grace period, created_at passthrough
- `src/dgov/agents.py` — pi stdin transport
- `~/.dgov/agents.toml` — stdin transport + --model flags for all River agents
- `.dgov/plans/selftest.toml` — repeatable system self-test
- `CLAUDE.md` — session-start checklist
- `CODEBASE.md` — LLM-native format, auto-refreshed on dispatch
