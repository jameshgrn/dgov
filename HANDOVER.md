# Handover: 5-parallel Kimi dispatch validated, 26 mypy errors eliminated

## Session context
- Date: 2026-03-29
- Branch: main @ 3e59528
- Last commit: fix: restore decision_providers.py from stash corruption

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| — | — | No active panes |

## Open bugs/issues
- #202: alias_for entries in project .dgov/agents.toml are dead code (medium)
  - load_routing_tables() only reads entries with 'backends' key, silently ignores alias_for
  - Fix: either implement alias resolution or replace with proper backends entries

## Blockers/debt
- None blocking

## Next steps
1. Fix ledger #202 (alias_for dead code) — low priority, routing works via matrix keys directly
2. Investigate retry-that-vanished pattern: DAG run 129 unit `fix-decision-batch-types` retry a2 completed but was closed/pruned without review or merge. Recovery artifacts not preserved for retries.
3. Address governor UX gaps identified in debrief (see Notes below)

## Changes made this session
- **fix(routing)**: threaded `project_root` to `is_routable()` and `check_agent_cli()` — was blocking all plan dispatch with project-local routing keys (ledger #201, fixed)
- **26 mypy errors → 0**: 5-unit plan dispatched to Kimi K2.5 pool, 4/5 merged autonomously, 1 governor micro-fixed
- **2 pre-existing test failures fixed**: merger coverage tmux title mock, stale protected-damage assertion
- **Dead test file removed**: test_plan_wait.py referenced deleted `_wait_for_dag`
- **ReviewOutputDecision.verdict widened** from `ReviewVerdict` to `str` — verdicts include raw model strings beyond enum members
- **CI fully green**: ruff, mypy, 1848 tests passing

## Notes

### Governor debrief — what works, what doesn't

**Works well:**
- Plan system is the right abstraction — TOML spec with file claims, evals, agent routing compiles to DAG
- File claims prevented all merge conflicts across 5 parallel workers
- Monitor daemon driving lifecycle was mostly invisible when working
- Router degradation typing (DegradationReason, DegradationState) is clean infrastructure

**Broken/gaps:**
- `is_routable()` config scope mismatch was critical-path blocker (fixed this session)
- Retry lifecycle: retry a2 completed but DAG finalized before monitor processed review — work silently lost
- Kimi wrote `alias_for` config that looked correct but was dead code — no validation caught it
- All 5 workers resolved to kimi-k25-0 (no per-agent max_concurrent, pool is cosmetic)

**Architectural observations:**
- Monitor does too much (DAG lifecycle + events + auto-merge + retries + classification + cleanup) — hard to diagnose failures
- Governor has no event stream — reduced to sleep+poll despite "no polling" rule
- Routing matrix (DecisionKind, CapabilityTier) is over-engineered for current usage — plans use agent names not decision kinds, matrix lives in docs not dispatch code
- `generate-t3` is manually encoding what should be `(kind=GENERATE, tier=T3)` natively

**Desired improvements (priority order):**
1. Event subscription for governor: `dgov wait-any --dag <id>` blocking on named pipe
2. Retry artifact preservation: worktree/diff survives until DAG finalizes
3. Native (kind, tier) dispatch in plans instead of routing key strings
4. Post-mortem in `dgov dag status`: show why each failed unit failed (last event, verdict, error)
