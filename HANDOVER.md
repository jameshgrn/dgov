# Handover: architectural discipline pass on state, DAG contracts, and recovery

## Session context
- Date: 2026-03-29T20:17:57Z
- Branch: main @ f790df0
- Last commit: Merge r162-decouple-title-update

## Open panes
None.

## Open bugs/issues
- #237: kimi-k25 resolves to lt-gov role via routing table; workers dispatched as lt-gov role unexpectedly (high)
- #235: DAG dependency ordering not enforced at dispatch (medium)
- #234: DAG run 156 done->merged transition error in `_finalize_merged_pane` (high)
- #233: DAG run 157 dispatched panes but pane records missing from DB (medium)

## Blockers/debt
- Working tree is intentionally dirty with an uncommitted architectural cleanup pass across status, persistence, recovery, DAG, batch, API, and tests.
- `review_fix` still manually reimplements orchestration instead of compiling to the canonical plan/DAG pipeline.
- Routing cleanup is incomplete. Some code paths still hard-code physical agent/model names outside the router, especially in decision/provider and merger-adjacent code.
- Docs and prompt artifacts still drift from live behavior.

## Next steps
1. Review and commit the current architectural pass after one more focused read of [src/dgov/persistence.py](/Users/jakegearon/projects/dgov/src/dgov/persistence.py), [src/dgov/status.py](/Users/jakegearon/projects/dgov/src/dgov/status.py), [src/dgov/recovery.py](/Users/jakegearon/projects/dgov/src/dgov/recovery.py), and [src/dgov/cli/pane.py](/Users/jakegearon/projects/dgov/src/dgov/cli/pane.py).
2. Finish routing discipline by removing remaining hard-coded physical agent names from orchestration-facing modules and update tests accordingly.
3. Converge [src/dgov/review_fix.py](/Users/jakegearon/projects/dgov/src/dgov/review_fix.py) onto the canonical plan/DAG execution path or explicitly retire it.
4. Update stale docs and prompt artifacts after code paths are stable.

## Notes
- Completed this session:
  state/reporting split: `list_worker_panes()` is now read-only and no longer settles pane state.
  typed DAG contracts: `dag_tasks` now persist `file_claims` and `commit_message`; cross-plan checks and retry contract recovery query typed rows instead of reparsing `definition_json`.
  recovery policy: retry/escalate preserve replaced panes by default for inspection.
  legacy convergence: `pane batch` now routes through canonical batch/DAG execution.
  role defaults: public/template defaults now prefer logical roles (`worker`, `supervisor`, `manager`) instead of advertising physical model names.
- Targeted verification completed successfully:
  `uv run ruff check src/dgov/persistence.py src/dgov/status.py src/dgov/monitor.py src/dgov/recovery.py src/dgov/dag.py src/dgov/executor.py src/dgov/plan.py src/dgov/cli/plan_cmd.py src/dgov/cli/pane.py src/dgov/batch.py src/dgov/templates.py src/dgov/api.py tests/test_dgov_panes.py tests/test_dgov_state.py tests/test_done_strategy.py tests/test_plan.py tests/test_batch.py tests/test_api.py tests/test_templates.py`
  `UV_NO_SYNC=1 uv run pytest tests/test_dgov_state.py tests/test_plan.py tests/test_done_strategy.py tests/test_dgov_panes.py tests/test_recovery_dogfood.py tests/test_batch.py tests/test_api.py tests/test_templates.py -q -m unit -k 'dag_task or cross_plan or ListWorkerPanes or RetryWorkerPane or EscalateWorkerPane or read_only or done_signal or batch or Orchestrator or template'`
  Result: `84 passed, 307 deselected in 15.36s`
- Modified files currently in the working tree:
  `src/dgov/api.py`
  `src/dgov/batch.py`
  `src/dgov/cli/pane.py`
  `src/dgov/cli/plan_cmd.py`
  `src/dgov/dag.py`
  `src/dgov/executor.py`
  `src/dgov/monitor.py`
  `src/dgov/persistence.py`
  `src/dgov/plan.py`
  `src/dgov/recovery.py`
  `src/dgov/status.py`
  `src/dgov/templates.py`
  `tests/test_dgov_panes.py`
  `tests/test_dgov_state.py`
  `tests/test_done_strategy.py`
  `tests/test_plan.py`
  `tests/test_templates.py`
- `src/dgov/monitor.py` already had a local behavior change before this handover session. It was preserved during edits.
