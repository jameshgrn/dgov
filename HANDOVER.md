# Handover: State audit — PaneState enum, model decomposition, AgentDef in progress

## Session context
- Date: 2026-03-29
- Branch: main @ 1432a2f
- Last commit: Merge merge-success-tests

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| r136-define-subobjects | active | AgentDef decomposition into PromptTransport/HealthConfig/RetryConfig (P3 plan run 136) |

## Open bugs/issues
- #215: DAG state tracking: tasks show dispatched after merge — kernel state not advancing through monitor (high)
  - Reproduced on runs 130-134. Monitor merges panes but kernel doesn't receive TaskMergeDone/TaskClosed events
  - Workaround: manually dispatch dependent units when DAG stalls
- #219: Worktree removed before pane land — done state triggers cleanup before merge/close can run (high)
  - Hit 3 times on executor enum fix. Worker completes → worktree deleted → land fails with FileNotFoundError
  - Root cause: auto-cleanup races with governor landing flow

## Open debt
- #216: Monitor phase classification returns "unknown" for pi/kimi workers during startup warmup (low)
  - Likely the pi init sequence before first tool call (ledger #220)

## Blockers/debt
- None blocking current work

## Next steps
1. **P3 AgentDef decomposition** — run 136 in progress. Foundation unit (define-subobjects) is active. 3 dependent units pending: update-lifecycle, update-health-consumers, update-retry-consumers. May need manual dispatch due to bug #215.
2. **Fix #215 (DAG state tracking)** — investigate monitor→kernel event flow. `_drive_dag` processes kernel actions but completion events may not feed back to `kernel.handle()`.
3. **Fix #219 (worktree race)** — done state should NOT trigger worktree removal. Worktree must survive until close.
4. **Remaining PaneState refs** — 13 raw strings left across 7 files (mostly DAG statuses and signal types, ~6 genuine misses)
5. **State audit: next targets** — executor review policy if/elif (line ~1259), recovery dispatch table (line ~814)

## Changes made this session

### Clanker discipline / state audit (3 plans)

**P1: PaneState StrEnum (plan run 134, 12 units)**
- Defined `PaneState` (12 members) and `PaneTier` (3 members) StrEnums in persistence.py
- Propagated across 29 source files: 347 → 13 raw string refs remaining
- 10 plan units + 2 fix-forward dispatches for executor.py and status.py+lifecycle.py

**P2: Model decomposition (plan run 135, 4 units)**
- Removed ReviewInfo InitVar shims + 16 property aliases — callers use sub-objects (tests/freshness_info/automation/contract)
- Added DoneStrategyType StrEnum (5 members) replacing raw string type field
- Extracted MergeSuccess test fields into shared ReviewTests sub-object

**P3: AgentDef decomposition (plan run 136, in progress)**
- Extracting PromptTransport, HealthConfig, RetryConfig from 25-field AgentDef grab-bag
- Foundation unit dispatched, 3 consumer units pending

### Governor micro-edits (2)
- Fixed 2 missed PaneState refs in status.py
- Updated test_review_fix.py for MergeSuccess.tests sub-object

### Operational
- 5 ledger entries: patterns #217 (TYPE_CHECKING import), #218 (over-generalized replacement), #220 (unknown phase = startup); bug #219 (worktree race); fix #221 (governor polling)
- 3 memory entries: no-shims feedback, state audit frequency feedback, worker sub-delegation project note

## Notes

### Worker failure patterns this session
- **TYPE_CHECKING import** (ledger #217): Kimi workers put runtime imports inside TYPE_CHECKING block. Always specify "OUTSIDE TYPE_CHECKING" in prompts.
- **Over-generalized replacement** (ledger #218): Workers converted "completed" and "review_pending" to PaneState members — those aren't pane states. Must explicitly list exclusions.
- **Worktree race** (bug #219): Workers that complete in <15s get their worktree cleaned before governor can land. The monitor's auto-close is too aggressive.

### DAG kernel stalls (bug #215)
Plans 134 and 135 both hit this. All tasks show "dispatched" in DAG status even after merge. Dependent units don't auto-dispatch — governor must manually dispatch them. The kernel's task_states dict freezes because it never receives completion events from the monitor.

### Renamed skill reference
User wants the "clanker discipline" skill renamed to "state audit" or similar. The principles are the same: derive-don't-store, make-wrong-states-impossible, enforce-function-contracts, data-over-procedure.
