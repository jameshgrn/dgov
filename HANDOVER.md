# Handover: State audit complete, 3 plans landed, 2 safety layers + bug fix

## Session context
- Date: 2026-03-29
- Branch: main @ 2759134
- Last commit: Merge fix-219-landing-flag

## Open panes
None.

## Open bugs/issues
- #215: DAG state tracking: tasks show dispatched after merge — kernel state not advancing through monitor (high)
  - Reproduced on runs 130-135. Monitor merges panes but kernel doesn't receive completion events
  - Intermittent — run 136 worked correctly, earlier runs stalled

## Open debt
- #224: Ad-hoc pane create uses different merge path than plan units — no governor notification, violates one-canonical-pipeline rule (medium)
  - Fix: ad-hoc dispatch should create single-unit plan under the hood so everything flows through the DAG kernel
  - This is the next task

## Blockers/debt
- None blocking

## Next steps
1. **Unify ad-hoc dispatch into plan pipeline** (debt #224) — `pane create` should generate a single-unit plan TOML and run it through `plan run`. Eliminates the dual merge path and gives governor notification for all dispatches. This was flagged as a policy violation ("one canonical pipeline, no drift").
2. **Fix #215 (DAG state tracking)** — investigate monitor→kernel event flow. The kernel's task_states dict freezes because it never receives TaskMergeDone/TaskClosed events from the monitor.
3. **State audit next pass** — remaining 13 raw PaneState strings (mostly DAG/batch statuses), executor review policy table, recovery dispatch table

## Changes made this session

### State audit — 3 plans executed

**P1: PaneState StrEnum (plan run 134, 12 units)**
- Defined PaneState (12 members) and PaneTier (3 members) StrEnums in persistence.py
- Propagated across 29 source files: 347 → 13 raw string refs remaining
- 10 plan units + 2 fix-forward dispatches

**P2: Model decomposition (plan run 135, 4 units)**
- Removed ReviewInfo InitVar shims + 16 property aliases — callers use sub-objects
- Added DoneStrategyType StrEnum (5 members)
- Extracted MergeSuccess test fields into shared ReviewTests sub-object

**P3: AgentDef decomposition (plan run 136, 5 units)**
- Extracted PromptTransport, HealthConfig, RetryConfig from 25-field AgentDef
- Foundation worker updated agents.py + lifecycle.py + preflight.py in one commit
- Governor micro-fixed router.py + cli/admin.py, dispatched test fix worker for 9 test files

### Safety layers (adversarial-reviewed by codex-mini agent)

**Alt C: ty check in post-merge validation** (merger.py)
- Runs `ty check` on changed Python files after every merge
- Non-blocking (warning only) — catches AttributeError from restructured dataclasses

**Alt B: Plan validator write/read overlap warning** (plan.py)
- Warns when unit A edits files that unit B reads without a dependency edge
- Catches the exact pattern that broke dgov during AgentDef decomposition

### Bug fix

**#219: Worktree removed before pane land** (monitor.py) — FIXED
- Root cause: `_try_auto_merge` didn't set `landing=True` flag before calling `run_land_only`
- Fix: set landing flag before merge, clear after — prevents prune_stale_panes from deleting worktree mid-merge

### Operational ledger entries
- #217: Kimi workers put runtime imports inside TYPE_CHECKING block
- #218: Kimi workers over-generalize string replacement
- #219: Worktree race (FIXED)
- #220: Monitor "unknown" phase is startup warmup
- #221: Governor polling violation (self-corrected)
- #222: AgentDef decomposition broke dispatch path — must update all consumers atomically
- #223: Ad-hoc panes have no governor notification
- #224: Dual merge path violates canonical pipeline rule

## Notes

### Adversarial review process
Codex-mini agent reviewed the import safety design and rejected all 3 proposed layers in favor of simpler alternatives (ty check + plan validator overlap warning). Key insight: "Layer 1 is wrong because you can't un-merge — detection after the fact doesn't help. Prevention at plan time or a type checker that catches the bug class is strictly better." The review process worked well — should use it for all non-trivial designs.

### Monitor reliability
The monitor crashed mid-session (no status.json heartbeat) after code changes. Workers completed but stayed in "active/working" state because the monitor wasn't polling. Fixed by restarting monitor. The stale-code-hash detection works for plan runs but not for ad-hoc dispatches that happen between restarts.

### Worker performance this session
- Kimi K2.5 via Fire Pass: ~15 workers dispatched, most completing in 30s-2min
- Failure modes: TYPE_CHECKING imports (2x), over-generalized replacements (2x), silent exit with 0 commits (1x)
- Success rate: ~80% first attempt, 100% with retry + refined prompts
