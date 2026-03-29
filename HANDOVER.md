# Handover: Clean board — 15 fixes, unified dispatch, state audit complete

## Session context
- Date: 2026-03-29
- Branch: main @ ee4c220
- Last commit: Add missing DagTaskState import to batch.py
- Governor: opus-i

## Open panes
None.

## Open bugs/issues
None.

## Blockers/debt
None open.

## Next steps
1. **Verify DAG kernel reconciliation works** — run a real multi-unit plan and confirm `dgov dag status` shows correct task states (not stuck at "dispatched"). The reconciliation code landed but wasn't validated end-to-end due to the chicken-and-egg with #215/#216.
2. **Test DGOV_COMMIT_MSG in production** — next plan dispatch should produce proper commit messages from plan unit `commit_message` fields instead of "Auto-commit on agent exit".
3. **Test preflight auto-commit** — make a governor micro-edit, then dispatch a worker touching the same file. Should auto-commit and pass preflight.
4. **Push to remote** when ready — 27 commits since last push, all tests pass (1883), zero lint.
5. **Phase 4 remaining work** — span-based monitor alerts (4c), test coverage push (4d).
6. **Consider**: the `dgov dag wait <run-id>` command enables `dgov plan run --wait` to be re-implemented cleanly if desired.

## Changes made this session

### Bugs fixed
- **#215** (high): Added `pane_merged` to `_DAG_EVENT_FACTORY` — kernel was missing merge events from monitor
- **#216** (low): Added headless worker patterns (done/working) to `DETERMINISTIC_PATTERNS` for pi/kimi agents
- **#224** (medium): Unified `pane create` worker path through `build_adhoc_plan` → `run_plan`, eliminating dual merge path. LT-GOV retains direct dispatch.

### Governor friction fixes (5)
- **DGOV_COMMIT_MSG**: Auto-commit wrapper reads `$DGOV_COMMIT_MSG` env var (falls back to generic). Lifecycle exports it from `packet.commit_message`.
- **`dgov dag wait <run-id>`**: New command blocks on event-driven pipes until DAG reaches terminal state.
- **Monitor reconciliation**: `_reconcile_kernel_from_journal()` replays missed events from journal on DAG load.
- **Preflight auto-commit**: `_auto_commit_governor_changes()` commits governor edits before git_clean check.
- **Grep BRE warning**: Plan validator only warns about `\|` in `grep -E` (ERE), not plain `grep` (BRE).

### State audit (2 rounds, fully clean)
- **Round 1**: 5 raw PaneState strings (4 files, Kimi swarm) + 7 type annotation gaps (5 files, Kimi swarm)
- **Round 2**: 11 raw state strings, 4 sentinels, 6 type hints across executor, batch, dag, lifecycle, kernel, spans, terrain, decision_providers (5 Kimi workers)
- **Final audit**: Zero violations across all 8 clanker-discipline categories

### Infrastructure
- **Missive category** added to operational ledger — governor lineage via `dgov ledger add missive "..." -t <name>`
- **`serialize_plan()` rewritten with `tomli_w`** — no more hand-rolled TOML, automatic escaping
- **Plan built programmatically** for round 2 audit — dogfooded `serialize_plan()` from Python

### Test suite
- 1883 unit tests passing, 0 failures
- Updated CLI/template/preflight tests for plan-based dispatch path

## Notes

### Governor missive (opus-i, #225)
"Trust the workers. Fix the plumbing. Three bugs cleared, zero open — leave it cleaner than you found it."

### Kimi K2.5 performance
- ~20 workers dispatched this session via Fire Pass
- Phase detection (#216 fix) working: all workers correctly classified as "working" then "done"
- Commit messages (#DGOV_COMMIT_MSG) working on later dispatches
- One worker missed (commit-msg unit) — wrote to wrong file. Governor micro-edited the fix.
- Swarm parallelism effective: 4-5 workers completing in 30-45s each

### DAG kernel state still lagging
The `pane_merged` factory fix and reconciliation code landed, but the kernel-shows-"dispatched" problem persisted for all DAG runs this session. The reconciliation runs on DAG load but may not fire if the monitor doesn't reload the DAG run. Needs end-to-end validation next session.

### Tunnel
River GPU tunnel was unreachable all session. All work done via Kimi K2.5 (Fireworks Fire Pass). Run `dgov tunnel` at next session start.
