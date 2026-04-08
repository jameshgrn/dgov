# Handover: Stress-Test & Bug-Fix Session

**Date:** 2026-04-07
**Branch:** `main` @ `e9b94ca0`
**Context:** Systematic stress-test of dgov before public release. Code audit + live dogfood run surfaced 6 bugs, all fixed and pushed.

---

## Current State

- `main` is clean and pushed. 60 tests pass (settlement + integration + runner).
- Stress-test plan ran end-to-end: 3 parallel tasks + 1 downstream, all 4 merged, sentrux clean.
- HANDOVER.md, runs.log, deployed.jsonl, and sentrux baseline updated.

---

## Bugs Fixed This Session

| Bug | File(s) | Notes |
|-----|---------|-------|
| Silent "complete" on crash+re-run | `cli/run.py` | ABANDONED/skipped now surface; warns about `--continue` |
| `kernel.status = COMPLETED` for all-ABANDONED DAG | `kernel.py` | ABANDONED+TIMED_OUT included in `has_failed` check |
| DB retry could hang ~200s under high concurrency | `persistence/connection.py` | 20 linear retries → 5 constant 0.25s retries |
| `scope_violation` on tasks creating files in new dirs | `settlement.py` | `git status --untracked-files=all` lists files not dir markers |
| Review gate errors silent in CLI output | `runner.py` | `review_fail` verdict stored in `_task_errors` |
| Settlement errors silent in CLI output | `runner.py` | `_merge()` error stored in `_task_errors` |
| Missing DB sync test coverage | `test_integration.py` | 5 new tests: fail/merge DB state, orphan×2, downstream skip |

---

## Key Decisions

- **Orphan behavior on bare re-run is intentional**: ACTIVE tasks become ABANDONED, cascade to SKIPPED. CLI now warns with `--continue` hint. The silent "complete" was the bug.
- **Scratch files in git-tracked dirs trigger sentrux**: Even simple data files increase complexity metrics. Use `.txt` or constant-only `.py` for scratch content, or put scratch outside tracked tree.
- **`DagDone` is NOT dead code**: `handle()` wrapper appends `_summary()` when `done=True`. Initial analysis was wrong.

---

## Open Issues (Carried Forward)

- **Runner contention under 6+ parallel tasks**: `_run_dispatch_action` / `ThreadPoolExecutor` interaction under large concurrent plans still undiagnosed.
- **No resume/checkpoint**: crash at task 7/10 = restart with `--continue`. Lost work is re-run.
- **No token/cost tracking**: no visibility into API spend per run.
- **Sentrux gate too strict on complexity**: any `Complex functions increased` triggers rejection. Workers can't add new helper functions without hitting the gate. Should warn, not hard-fail.

---

## Next Steps

1. **Sentrux complexity gate**: treat complexity increases as warnings unless quality score drops.
2. **Token/cost tracking**: add token counts to `_append_run_log` and exit summary.
3. **Investigate runner contention**: profile under 8+ concurrent tasks.

---

## Important Files

- `/Users/jakegearon/projects/dgov/src/dgov/settlement.py` — `_get_all_changes` now uses `--untracked-files=all`
- `/Users/jakegearon/projects/dgov/src/dgov/runner.py` — review + settlement errors in `_task_errors`
- `/Users/jakegearon/projects/dgov/src/dgov/kernel.py` — `status` property with ABANDONED in `has_failed`
- `/Users/jakegearon/projects/dgov/src/dgov/persistence/connection.py` — `_LOCK_RETRIES=5`, constant backoff
- `/Users/jakegearon/projects/dgov/src/dgov/cli/run.py` — abandoned/skipped surfaced in output
- `/Users/jakegearon/projects/dgov/tests/test_integration.py` — `TestDbStateSync`, `TestOrphanAbandon`
