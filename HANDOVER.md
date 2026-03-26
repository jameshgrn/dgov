# HANDOVER

## Current State
- Branch: `main` at `0f98a19` (dirty working tree)
- Tests: targeted unit slice `211` passed, `0` failed; full suite not rerun per project policy
- Panes: none

## Completed This Session
- **Fix dict operations on ReviewInfo/PaneMergeResult** (`16b13f5`): replaced dict-style access at pane/executor callsites with dataclass-aware handling.
- **Fix conflict marker false positive** (`a6cd21b`): `_check_conflict_markers()` now matches real conflict-marker lines instead of comment separators.
- **Refactor PaneMergeResult → discriminated union** (`925ce9e`): split merge outcomes into `MergeSuccess | MergeError | MergeConflict` and updated consumers/tests.
- **Clean up superseded panes on retry** (`ec58f06`): retry now closes old pane artifacts after superseding, matching escalate behavior.
- **Fix 4 test failures from discriminated union** (`0f98a19`): repaired duck-typed merge assertions and dataclass-vs-dict test mocks.

## Ledger Snapshot
### Open Debt
- #134 — `lifecycle.py _full_cleanup` uses `None` as worktree-removal tri-state (low)
- #133 — `decision.py ConsensusProvider._call` keeps optional result/error pairs (low)
- #132 — `merger.py _check_merge_preconditions` is still branchy/data-shaped logic (low)
- #131 — `inspection.py ReviewInfo` is still a 20+ field grab-bag and should be split by phase/composition (medium)
- #130 — executor optional-bag result debt is now fixed in the working tree but still open in ledger; resolve after commit (medium)
### Rules
- #118 — `landing` field stays on pane; live coordination flag, not derivable from events
- #88 — verify monkeypatch target after imports move
- #87 — governor commits own changes before dispatching workers that touch same file
- #86 — Pi workers only see CLI args + system prompt; context must be appended explicitly
- #56 — OpenRouter is only for open-source models
- #27 — `wait_worker_pane` stays event-driven; no polling
- #14 — kernel is pure: no subprocess, no I/O, no blocking
- #13 — every state transition must emit an event

## Lookup Cache
- `src/dgov/executor.py:36-177` — executor result layer now uses explicit outcome variants (`PostDispatchOutcome`, wait/review/land unions) with compatibility properties instead of optional-bag dataclasses.
- `src/dgov/executor.py:180-387` — `ReviewOnlyResult`, `WaitOnlyResult`, `MergeOnlyResult`, `ReviewMergeResult`, and `LandResult` now encode valid states directly; merge errors derive from `PaneMergeResult`.
- `src/dgov/executor.py:703-856` — `run_wait_only()` now constructs explicit success/failure wait outcomes; timeout/recovery paths no longer synthesize bag states.
- `src/dgov/executor.py:997-1170` — review/merge/land flows now return variant-backed results rather than partially populated option bags.
- `src/dgov/executor.py:2127-2690` — DAG runtime adapter is fully typed with `DagDefinition`, `DagAction`, and `DagEvent`; fallback `DagRunResult` now includes `blocked`.
- `src/dgov/api.py:155-235` — API merge/land methods now inspect `MergeSuccess` explicitly instead of dict-shaped merge payloads.
- `src/dgov/monitor.py:158-175` — `_DAG_EVENT_FACTORY` is now typed and normalizes `dag_resumed` actions through `GovernorAction`.
- `src/dgov/monitor.py:434-520` — `_drive_dag()` now consumes typed DAG actions/events and updates pane-slug mapping from the event payload, not the wider action union.
- `src/dgov/review_fix.py:340-346` — review-fix success path now treats only `MergeSuccess` as merged.
- `tests/test_retry.py:380-407` — CLI wait tests now build `WaitOnlyResult` via `completed()` instead of the removed bag constructor.
- `.claude/skills/dgov/SKILL.md` — still current on some commands, but stale on role/model guidance and on treating `pane create --land` as the default path.
- `.claude/commands/dgov-handover.md` — still requires a full unit-suite health check, which conflicts with repo policy for normal governor work.
- `.claude/commands/dgov-dispatch.md` — still tells governors to choose concrete model names and always use `pane create --land`; policy now prefers roles and `plan run` for non-micro work.
- `.claude/commands/dgov-debrief.md` — `dgov agent stats` command is valid; no obvious command-name drift in the file.

## Open Issues
- Uncommitted working tree contains the executor clanker follow-through in `src/dgov/executor.py`, `src/dgov/api.py`, `src/dgov/monitor.py`, `src/dgov/review_fix.py`, and `tests/test_retry.py`; review and commit before dispatching workers that touch those files.
- `inspection.py:40-75` — `ReviewInfo` is still the highest-signal unfinished clanker target. Recommended fix: split core review state from freshness/test/manifest metadata via phased composition or explicit review variants.
- Ledger debt #130 is stale after the uncommitted executor refactor. Resolve it only after the working tree is committed so ledger state matches history.
- Skill `/dgov` is stale: it still says default worker routing is `qwen-9b` / `--agent qwen-35b` and centers `pane create --land`; it should describe role-based routing and `uv run dgov plan run` as the default path per `CLAUDE.md`.
- Skill `/dgov-dispatch` is stale: it instructs governors to choose concrete models and always emit `pane create --land`; it should prefer roles and reserve `pane create` for micro-tasks.
- Skill `/dgov-handover` is stale: Step 1 runs the full unit suite, which conflicts with repo policy outside push-time CI. It should use latest targeted verification or explicitly note when full-suite execution is intentionally skipped.
- `dgov status` still reports `18 healthy / 4 unhealthy` agents. Investigate before relying on escalation paths or tunnel-dependent workers.

## Next Steps
- Review and commit the current executor/monitor/API/test working tree, then resolve ledger debt #130.
- Refactor `ReviewInfo` in `src/dgov/inspection.py` to finish the main remaining clanker-discipline audit item.
- Update `.claude/skills/dgov/SKILL.md`, `.claude/commands/dgov-dispatch.md`, and `.claude/commands/dgov-handover.md` so Claude-side process docs match current role-based governor policy.
- Investigate unhealthy agents from `uv run dgov status -r .` before dispatching new work that depends on escalation or local routing.
