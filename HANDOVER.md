# HANDOVER

## Current State
- Branch: `main` at `2981022` (clean working tree)
- Tests: 1843 passed (full unit suite), 61 lifecycle, 35 merge, 39 status, 7 persistence-pane — all clean
- Panes: none
- Status: 0 panes, 23 agents (16 healthy, 7 unhealthy), 3 recent failures, 2 open bugs

## Completed This Session
- **Bug #185 - Fix readonly phase timeout** (`31edd8a`): Fixed `_dag_wait_any` to detect workers stuck in non-terminal phases (STUCK, IDLE, WAITING_INPUT) and emit `timed_out` after `readonly_timeout` (default 30s). Added tests for timeout detection in all readonly phases.
- **Pane close ownership guard** (`2981022`): `close_worker_pane` checks tmux pane title contains slug before killing. Prevents fratricide on reused pane IDs. Bug #193.
- **Candidate worktree elimination** (`49eea2f`): Merge pipeline uses `merge-tree --write-tree` directly on main. No candidate worktree, no venv creation tax. Non-Python merges: ~6.9s -> <1s. Fixed macOS flock deadlock.

## Ledger Snapshot
### Open Bugs
- #185 — Worker plan tasks can stall indefinitely in read-only phase without emitting a timeout event (medium)
- #184 — Cancelled DAG runs can still leave retry descendants alive or stale running tasks (medium)
### Resolved This Session
- #187 — Terminal DAG cleanup leaves superseded ancestors (fixed)
- #190 — Symlink shim replaced by worktree relocation (superseded by #191)
- #193 — close_worker_pane kills unrelated process on pane_id reuse (fixed)

## Lookup Cache
- `/Users/jakegearon/projects/dgov/src/dgov/status.py:514-527` — superseded/escalated force-prune in `prune_stale_panes`
- `/Users/jakegearon/projects/dgov/src/dgov/status.py:544-571` — orphan scan covers `~/.dgov/worktrees/<hash>/` + legacy `.dgov/worktrees/`
- `/Users/jakegearon/projects/dgov/src/dgov/persistence.py:755-763` — `_get_db` schema version gate (`PRAGMA user_version`)
- `/Users/jakegearon/projects/dgov/src/dgov/persistence.py:647-736` — `_migrate_sentinels_to_null` (gated, runs once)
- `/Users/jakegearon/projects/dgov/src/dgov/cli/admin.py:490-512` — parallel health checks in `dgov status`
- `/Users/jakegearon/projects/dgov/src/dgov/lifecycle.py:266-277` — `_worktree_base()`: `~/.dgov/worktrees/<sha256[:12]>/`
- `/Users/jakegearon/projects/dgov/src/dgov/lifecycle.py:325-327` — `_find_unique_slug` uses `_worktree_base()`
- `/Users/jakegearon/projects/dgov/src/dgov/lifecycle.py:1160-1202` — pane close ownership guard (title check before kill)
- `/Users/jakegearon/projects/dgov/src/dgov/merger.py:1599-1707` — merge pipeline: no candidate worktree, merge-on-main + revert-on-failure
- `/Users/jakegearon/projects/dgov/src/dgov/merger.py:301-460` — `_plumbing_merge`: merge-tree, commit-tree, update-ref (owns `_MergeLock`)
- `/Users/jakegearon/projects/dgov/src/dgov/merger.py:1218-1300` — `_validate_post_merge`: lint+tests, ~5s Python / instant non-Python
- `/Users/jakegearon/projects/dgov/src/dgov/merger.py:143-162` — `_MergeLock`: flock-based, deadlocks on macOS if nested same-file
- `/Users/jakegearon/projects/dgov/src/dgov/done.py:160-198` — `_wrap_cmd` auto-commit wrapper
- `/Users/jakegearon/projects/dgov/src/dgov/done.py:338-566` — `_is_done` signal priority chain
- `/Users/jakegearon/projects/dgov/src/dgov/recovery.py:50-71` — `_close_replaced_pane` 3-tier fallback
- `/Users/jakegearon/projects/dgov/src/dgov/monitor.py:1115-1129` — `_try_auto_merge` calls `run_land_only` (internal waiter can block)
- `/Users/jakegearon/projects/dgov/src/dgov/monitor.py:837-871` — `_process_candidate_set`: serial merge candidate loop
- `/Users/jakegearon/projects/dgov/.dgov/benchmarks/suite.toml` — 14 benchmark task definitions
- `/Users/jakegearon/projects/dgov/.dgov/benchmarks/run.py` — event-driven benchmark runner
- `/Users/jakegearon/projects/dgov/tests/test_lifecycle.py:31-42` — `_fake_tmux_run_for_slug()` test helper

## Open Issues
- **`--land` named pipe waiter blocks**: `run_land_only` uses per-process FIFOs that don't receive events from the monitor or done-detection (different processes). The benchmark runner works around this by polling the events table directly. The `--land` CLI path needs the same fix.
- **Monitor auto-merge is serial**: `_process_candidate_set` processes one merge at a time. Git safety requires serial merges on main, so the fix is per-merge speed (done) not parallelism.
- **`test_no_checkout_before_worktree_remove` pre-existing failure**: `tests/test_dgov_panes.py:1454`. Pre-dates this session.
- **Worker execution 65-77s for trivial tasks on MLX 9B**: inference speed, not plumbing. Not actionable at dgov layer.

## Next Steps
1. Fix `--land` waiter: replace named-pipe blocking with event-journal polling
2. Run full benchmark suite on clean tree to establish baselines
3. Address bug #184 (cancelled DAG retry descendants) and #185 (plan task read-only stall)
4. Investigate pre-existing `test_no_checkout_before_worktree_remove` failure
