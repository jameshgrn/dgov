# Handover: stabilized batch-test isolation and monitor DAG snapshot persistence

## Session context
- Date: 2026-03-29T21:29:55Z
- Branch: main @ cbd445c
- Last commit: Add handover state for 2026-03-29

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| r164-fix-dag-cli-root | reviewed_fail | Preserved evidence pane from run 164. Worker work completed, but review failed on a 120s test timeout and the historical DAG row remained stale. |

## Open bugs/issues
- #240: Pane batch/pytest temp plans can dispatch real DAG runs into the live governor session state and tmux session. High severity. The regression appears fixed in the current uncommitted working tree, but the ledger entry is still open because nothing has been committed or resolved yet.

## Blockers/debt
- Working tree is dirty with uncommitted stabilization fixes in:
  `src/dgov/dag_parser.py`
  `src/dgov/monitor.py`
  `tests/test_dgov_cli.py`
  `tests/test_monitor.py`
- Historical DAG run `164` is still stale in `.dgov/state.db`: `dag_runs.status` is `running` with `state_json` stuck at `idle/pending`, while `dag_tasks` and pane state advanced. This is preserved as evidence from before the monitor fix, not proof that the patched code still fails.
- Pane `r164-fix-dag-cli-root` remains in `reviewed_fail` and tmux pane `%160` is still present intentionally.

## Next steps
1. Review and commit the current stabilization fixes if they should be kept. Targeted verification already passed for CLI, batch, monitor, DAG override, lint, format, and `ty`.
2. Resolve ledger bug `#240` after commit, or reopen it with a narrower summary if more leakage cases remain.
3. Decide whether to repair or cancel historical DAG run `164`, or leave it as preserved postmortem evidence and open a separate bug for stale-run cleanup.
4. If resuming stabilization, continue with the next control-plane issue after these fixes: historical DAG reconciliation/cleanup around reviewed-fail runs and stale active run rows.

## Notes
- Direct fixes applied this session under the governor exception:
  `tests/test_dgov_cli.py`: pane-batch CLI tests now mock `dgov.batch.run_batch` instead of stale preflight/create-worker paths, preventing pytest temp plans from dispatching into the live session.
  `src/dgov/monitor.py`: `_drive_dag` now persists the kernel snapshot in a `finally` block so mid-pass failures cannot leave `dag_tasks` and `dag_runs.state_json` split.
  `src/dgov/dag_parser.py`: added `DagTaskSpec.all_touches()` as the canonical touched-file projection used by DAG persistence paths.
  `tests/test_monitor.py`: added a regression test that reproduces the split-brain shape by forcing a post-dispatch exception and asserting persisted `running/waiting/pane_slug` state.
- Targeted verification completed and passed:
  `uv run pytest /Users/jakegearon/projects/dgov/tests/test_dgov_cli.py -q -m unit`
  `uv run pytest /Users/jakegearon/projects/dgov/tests/test_batch.py -q -m unit`
  `uv run pytest /Users/jakegearon/projects/dgov/tests/test_monitor.py -q -m unit`
  `uv run pytest /Users/jakegearon/projects/dgov/tests/test_dag_overrides.py -q -m unit`
  `uv run ruff check /Users/jakegearon/projects/dgov/src/dgov/monitor.py /Users/jakegearon/projects/dgov/src/dgov/dag_parser.py /Users/jakegearon/projects/dgov/tests/test_dgov_cli.py /Users/jakegearon/projects/dgov/tests/test_monitor.py`
  `uv run ruff format --check /Users/jakegearon/projects/dgov/src/dgov/monitor.py /Users/jakegearon/projects/dgov/src/dgov/dag_parser.py /Users/jakegearon/projects/dgov/tests/test_dgov_cli.py /Users/jakegearon/projects/dgov/tests/test_monitor.py`
  `uv run ty check /Users/jakegearon/projects/dgov/src/dgov/monitor.py /Users/jakegearon/projects/dgov/src/dgov/dag_parser.py`
- Session cleanup completed:
  orphan tmux panes `%165` and `%166` were killed
  only tracked pane left is `r164-fix-dag-cli-root`
