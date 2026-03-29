# Handover: DAG reconciliation, monitor hardening, enum cleanup

## Session context
- Date: 2026-03-29
- Branch: main @ 5b3097a
- Last commit: Fix orphan worktree pruning and stale monitor restart
- Governor: opus-4.6

## Open panes
None.

## Open bugs/issues
None (0 open bugs, 0 open debt).

## Blockers/debt
None open.

## Next steps
1. **Phase 4c** — span-based monitor alerts. Design: monitor emits alert events when spans show anomalies (high failure rate, slow dispatch, repeated retries). No implementation yet.
2. **Phase 4d** — test coverage push. Identify coverage gaps in critical paths (kernel, monitor, executor) and dispatch test-writing workers.
3. **Kimi claim enforcement (ledger #230)** — Kimi workers edit files outside their declared claims. `--strict-claims` logs violations but doesn't block merge. Consider blocking by default.
4. **Push to remote** — 15+ commits this session. Board is clean. Run full CI suite before push.

## Changes made this session

### DAG kernel reconciliation (bugs #226, #227, #229)
- **dag_tasks sync (#226)** — `_drive_dag` in monitor.py now projects kernel `task_states` to `dag_tasks` table after each pass. `dgov dag status` shows real-time states (waiting, reviewing, merging, merged) instead of stuck `dispatched`.
- **Dispatch failure retry (#227)** — `TaskDispatchFailed` handler in kernel.py now retries (up to `max_retries`), then blocks on governor. Previously immediately set FAILED and skipped dependents.
- **DagTaskState.REVIEWED_FAIL (#229)** — nonexistent enum member referenced in dag.py `merge_dag`. Simplified to `DagTaskState.FAILED`.

### Raw string → StrEnum cleanup
- **monitor.py** — `WorkerPhase` enum in `_TERMINAL_RULES` (4 comparisons)
- **executor.py** — `DagTaskState`/`DagState` enums in `run_force_complete_dag`, `run_skip_dag_task` (with local imports added)
- **dag.py** — `DagTaskState.MERGED`, `DagState.COMPLETED`/`FAILED` in `merge_dag`

### Monitor hardening
- **Stale restart race (#231)** — `ensure_monitor_running` now polls for lock release (10×0.5s) after SIGTERM instead of blind `sleep(1)`. SIGKILL fallback after 5s. Prevents old monitor from processing events after being signaled.
- **Orphan worktree noise** — `_remove_worktree` falls back to `shutil.rmtree` for unregistered dirs. Prune scan skips dotfile entries. Cleaned 1636 stale test artifact dirs and 15 stale DAG runs.

### Other
- **Mermaid diagram updated** — added `active→escalated`, `abandoned→escalated`, `superseded→closed` with note. All 11 states represented.
- **DGOV_COMMIT_MSG validated** — plan `commit_message` flows through to squash commit on merge.
- **Forced review path verified** — 3/3 dispatches confirmed `pane_reviewed_pass` before `pane_merged`.
- **MEMORY.md cleaned** — removed 4 stale `--land`/`--wait` references per ledger rules #195/#198.
- **Kimi workers validated** — T3 parallel dispatch works. Claim violations logged (ledger #230).

## Notes

### Tunnel status
- Port 8083 (river-9b): healthy
- Ports 8080 (35B), 8082 (4B): down on remote
- Kimi (Fireworks): healthy, used for parallel dispatch

### Monitor is clean
- 0 active DAG runs, 0 active panes, 0 orphan noise
- Hash-based auto-restart mechanism verified end-to-end
- New monitor starts with correct code after merges

### Ledger entries this session
- #226 bug (fixed): dag_tasks not synced from kernel
- #227 bug (fixed): no retry on dispatch failure
- #228 bug (fixed): worker missed local import
- #229 bug (fixed): DagTaskState.REVIEWED_FAIL nonexistent
- #230 pattern (open): Kimi claim violations need enforcement
- #231 fix (fixed): stale monitor restart race
- #232 fix: monitor polls for lock release
