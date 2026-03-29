# Handover: State machine hardening, README overhaul, mypy zero

## Session context
- Date: 2026-03-29
- Branch: main @ 84868ee
- Last commit: Enforce mandatory review before merge, remove merge_conflict state
- Governor: opus-4.6

## Open panes
None.

## Open bugs/issues
None.

## Blockers/debt
None open.

## Next steps
1. **Verify forced review in production** — next `dgov pane create --land` dispatch should show `pane_reviewed_pass` event before merge. Confirm the new state path (done → reviewed_pass → merged) works end-to-end with real workers.
2. **DAG kernel reconciliation** — still unvalidated from prior session. Run a real multi-unit plan and confirm `dgov dag status` shows correct task states.
3. **Test DGOV_COMMIT_MSG in production** — still unvalidated from prior session.
4. **Phase 4 remaining** — span-based monitor alerts (4c), test coverage push (4d).
5. **Push to remote** — 4 commits pushed this session. Board is clean.

## Changes made this session

### State machine hardening (84868ee)
- **Removed `merge_conflict` state** — 12 states → 11. Merge conflicts are transient git conditions, not durable lifecycle states. Merger emits `pane_merge_failed` events with conflict details but no longer mutates pane state.
- **Forced review before merge** — removed `done→merged`, `active→merged`, `timed_out→merged` from VALID_TRANSITIONS. `run_review_merge` and `run_post_dispatch_lifecycle` now call `run_mark_reviewed(passed=True/False)` before merge. Added `pane_reviewed_pass` and `pane_reviewed_fail` event types.
- **`reviewed_pass→failed` replaces `reviewed_pass→merge_conflict`** — merge failures from reviewed_pass go to failed, not a separate conflict state.
- Updated README diagram, 13 files changed, 1883 tests passing.

### Mypy zero errors (8e2e63c)
- Fixed all 9 mypy errors across 6 files (executor, persistence, preflight, status, lifecycle, batch).
- Key bug found: `DagTaskState.REVIEWED_PASS` doesn't exist — was referencing a nonexistent enum member. Fixed to `MERGE_READY`.
- `state` variable in executor typed as `PaneState | str` to accommodate flow-control sentinels.

### README overhaul (5fe2c8c, e88bb5d)
- Added 40+ missing CLI commands across all subgroups (pane, plan, dag, agent, trace, config, etc.)
- Updated built-in agents table: added pi-* variants, switched from "Done detection" to "Transport" column
- Updated quick start with `dgov init` + plan workflow
- Fixed dependency claim: "one dependency (click)" → "four dependencies (click, rich, Pillow, tomli_w)"
- Simplified mermaid state diagram to show primary flows only, with note pointing to persistence.py for full table

## Notes

### State machine is now cleaner
The diagram tells the real story:
```
active → done → reviewed_pass → merged → closed
              → reviewed_fail → escalated → closed
       → failed → escalated → closed
       → timed_out → done (late) / escalated
       → abandoned → closed
```
No merge_conflict. No review bypass. 11 states, all edges enforced.

### Tunnel
River GPU tunnel still unreachable. All work done as governor micro-edits this session. Run `dgov tunnel` at next session start if workers needed.
