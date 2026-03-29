# Handover: 30+ fixes dogfooded through dgov, 1867 tests green

## Session context
- Date: 2026-03-29
- Branch: main @ 24c809c
- Last commit: Merge r133-test-router-spread

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| — | — | No active panes |

## Open bugs/issues
- #215: DAG state tracking: tasks show dispatched after merge — kernel state not advancing through monitor (high)
  - Reproduced on runs 130, 131, 132, 133. All tasks merge on main but DAG status still shows "dispatched"
  - The kernel's task_states dict is not being updated when the monitor processes merge events
  - Likely root cause: monitor drives lifecycle (review → merge → close) but doesn't feed TaskMergeDone/TaskClosed events back to the kernel, so kernel state freezes at "dispatched"

## Open debt
- #216: Monitor phase classification returns "unknown" for pi/kimi workers — output patterns don't match deterministic regexes (low)

## Blockers/debt
- None blocking

## Next steps
1. **Fix #215 (DAG state tracking)** — highest priority. Investigate the monitor→kernel event flow. The monitor merges panes but likely doesn't emit kernel events (TaskWaitDone, TaskReviewDone, TaskMergeDone) back to the DagKernel. Check `_drive_dag` and the DagReactor in monitor.py.
2. **Fix #216 (unknown phase)** — add pi/kimi output patterns to `_classify_deterministic` regex set in monitor.py. Low priority but visible UX gap.
3. **Event subscription** — `dgov wait-any --dag <id>` using existing named pipe infrastructure (ledger #214). Eliminates governor polling.

## Changes made this session

### Bug fixes (12)
- **alias_for resolution (#202)**: `load_routing_tables` now resolves `alias_for` entries to target route backends
- **Pool load balancing**: Router uses least-loaded selection instead of first-match for pool backends
- **Kernel TaskClosed race**: Handles TaskClosed during WAITING/REVIEWING states as failure
- **Monitor finalization race**: Defers DAG finalization while retry panes await review
- **Lifecycle worktree preservation**: Retry pane worktrees preserved during active DAG runs
- **DAG post-mortem**: `dag status` shows failure details (last event, verdict, error) for failed units
- **monitor_alive spam**: Heartbeat no longer emits events to journal — status.json timestamp is sufficient
- **pane create routing**: `is_routable()` now receives `project_root` — project-local routing keys (e.g. `generate-t3`) work
- **Preflight false positives**: Prompt-derived file touches downgraded to warnings, only explicit `--touch` claims block
- **Preflight touch detection**: `packet.file_claims` checked for explicit claims, not just `touches` param
- **Review rebase**: Worker branch rebased onto main before running tests — governor micro-edits visible
- **Review fd safety**: `stdin=subprocess.DEVNULL` in rebase and test subprocesses prevents Bad file descriptor errors from monitor daemon

### Features (1)
- **README updated** (via Gemini): Current CLI commands, architecture sections, eval-first planning

### Tests (+19)
- 1848 → 1867 tests: alias resolution (5), pool load balancing (3), DAG postmortem (5), kernel retry (1), inspection rebase (1), lifecycle DAG guard (2), monitor heartbeat (1), preflight derived (1)

### Operational
- 3 plan runs (130, 131/132, 133) totaling 26 Kimi worker dispatches
- 1 Gemini dispatch for README
- 4 governor micro-edits
- 6 ledger entries (fixes #204-#213, bug #215, debt #216)

## Notes

### Monitor restart required after code changes
The monitor daemon loads code at startup. Changes to `inspection.py`, `monitor.py`, etc. require monitor restart (`kill` + `dgov monitor --pane`). Run 131 failed because the monitor had stale code (pre-DEVNULL fix). Run 133 succeeded after restart.

### Plan validator doesn't check project-local routes
`dgov plan validate` warns "agent 'generate-t3' not found in registry and not routable" because the validator calls `is_routable()` without `project_root`. Same fix pattern as pane create — pass project_root. Low priority since plans execute correctly despite the warning.

### DAG state tracking root cause hypothesis
The monitor's `_drive_dag` processes kernel actions (DispatchTask, WaitForAny, etc.) but the action execution results (TaskWaitDone, TaskMergeDone) may not be fed back to `kernel.handle()`. The kernel's `task_states` dict freezes because it never receives the completion events. The actual pane lifecycle (merge, close) happens through the executor, but the kernel isn't informed. Check: does `_drive_dag` call `kernel.handle()` with events after action execution?
