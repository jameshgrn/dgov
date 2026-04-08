# Handover: Watch UI + Worker Tools

**Date:** 2026-04-08
**Branch:** `main` @ `f64381dc` (pushed)
**Context:** Merged `feature/watch-ui-worker-tools` — Rich-based watch UI and new worker capabilities.

---

## Current State

- `main` is clean and pushed. 532+ tests passing.
- Flat file claims feature shipped (prior handover).
- **Watch UI overhaul** merged: Rich tables, color-coded tasks, markdown summaries.
- **New worker tools** merged: `find_references()` and `revert_file()`.
- **Worker prompt v1.1.0**: Engaging tone, higher budgets (50 calls / 100 loop limit).

---

## Completed

### Watch UI Refactor (`src/dgov/cli/watch.py`)
- Rich tables with grid-aligned columns
- Task-specific color coding (8-color stable palette)
- Markdown rendering for worker summaries
- Agent name resolution from project config
- Cleaner slug display (strips `tasks/` prefix, `.toml` suffix)
- "New run" detection with color reset

### Worker Tools (`src/dgov/workers/atomic.py`)
| Tool | Purpose |
|------|---------|
| `find_references(symbol, exclude_tests=False)` | Symbol search via ripgrep/fallback to grep |
| `revert_file(path)` | Git checkout HEAD to restore file |

### Worker Prompt v1.1.0 (`src/dgov/worker.py`)
- Engaging "Greetings, Actuator" preamble
- Clear "DGOV Way" principles (Separation of Powers, Trust but Verify, Surgical Precision)
- Iteration budget: 50 tool calls, 100 loop limit
- Tactical guidance: check-in at call 40, max 3 test failure loops

### Infrastructure Fixes
- `pyproject.toml`: Per-file-ignores for `src/dgov/worker.py` E501 (prompt prose)
- `src/dgov/cli/watch.py`: Fixed `RenderableType` import path

---

## Key Decisions

- **Rich over plain text**: Significant UX improvement for monitoring plan execution. Tables auto-size, colors help distinguish concurrent tasks.
- **Tools are additive, not replacing**: Workers can now find symbol references and revert files when they go off track. Both use existing primitives (`run_bash`, git).
- **Budget increases are justified**: 30→50 calls and 60→100 loop limit based on observed worker behavior on complex tasks. 40-call check-in guidance prevents runaway exploration.
- **Worker personality matters**: The v1.1.0 prompt frames workers as "Actuators" in a "lineage of precise, surgical contributions." This appears to improve focus and reduce scope creep in testing.

---

## Open Issues (Carried Forward)

- **Runner contention under 6+ parallel tasks** — `ThreadPoolExecutor` interaction undiagnosed.
- **No token/cost tracking** — no visibility into API spend per run.
- **No semantic review** — `review_sandbox()` is git sanity checks only.
- **Sentrux scans scratch/test `.py` files** — any `.py` with functions in a git-tracked dir increases complexity count.

---

## Next Steps

### 1. Dogfood new tools
- Author a plan that uses `find_references` in a prompt.
- Verify workers can locate symbols across the codebase.
- Test `revert_file` recovery when a worker makes a bad change.

### 2. Token/cost tracking
- Workers emit token counts via `on_event` callback.
- Runner aggregates in `_task_durations`-style dict.
- `_append_run_log` + CLI exit summary include cost.

### 3. Runner contention profiling
- Add timing instrumentation around `ThreadPoolExecutor` calls in `_merge`.
- Run a plan with 8+ parallel tasks and check for >10s stalls.

---

## Important Files

| File | Role |
|------|------|
| `src/dgov/cli/watch.py` | Rich-based watch UI with tables, colors, markdown |
| `src/dgov/workers/atomic.py` | New tools: `find_references`, `revert_file` |
| `src/dgov/worker.py` | v1.1.0 prompt with DGOV Way principles, higher budgets |
| `pyproject.toml` | Ruff per-file-ignores for worker.py |
| `tests/test_cli.py` | Updated for Rich table output format |
| `tests/test_worker_tools.py` | Tests for `find_references` and `revert_file` |
