# Handover: Remove --wait and --land anti-pattern flags

## Session context
- Date: 2026-03-28
- Branch: main
- Last commit: b85b28b - Add dgov handover skill for pi agent

## Completed This Session
- **Removed --wait from plan commands** (`17d4d68`): Removed blocking `--wait` flag from `plan run`, `plan resume`, and `plan scaffold`. Also removed `_wait_for_dag()` helper function (~162 lines of blocking I/O code). Event-driven architecture restored.
- **Removed --land from pane create** (`7aeb8fd`): Removed `--land/--no-land` option and `--timeout` from `pane create`. The 50+ line inline lifecycle block that called `run_post_dispatch_lifecycle()` is gone.
- **Updated all references** (`e73eebb`, `47de741`, `69107f8`): Removed all `--wait` and `--land` references from:
  - Governor prompt template (`_GOVERNOR_PROMPT`)
  - LT-GOV plan instructions template
  - Worker prompt template
  - CLAUDE.md documentation
  - README.md documentation
  - `.claude/skills/dgov/SKILL.md`
  - `.claude/commands/dgov-dispatch.md`
  - Test assertions (`test_templates.py`)
  - Comments in `monitor.py`, `status.py`, `persistence.py`
- **Added dgov-handover skill for pi** (`b85b28b`): Created `.pi/skills/dgov-handover/SKILL.md` for parity with Claude's handover command.

## Open work
- No active panes
- Clean working tree

## Open bugs/issues (from ledger)
- #194: DAG status tracking: claim_violation events not mapped to kernel DagEvents (high)
- #185: Worker plan tasks can stall indefinitely in read-only phase (medium)
- #184: Cancelled DAG runs can leave retry descendants alive (medium)

## Blockers/debt
- Bug #194 (claim_violation) needs a plan written and executed. This is the priority bug.

## Next steps
1. Fix bug #194: Write plan for claim_violation DAG event mapping, run it
2. Review any stale bugs (#185, #184) and resolve or escalate
3. Ensure all tests pass after the flag removals

## Notes
- The anti-pattern flags are completely excised from the codebase. Zero occurrences in source (excluding HANDOVER.md historical context).
- All 27 template tests pass with updated negative assertions.
- `dgov pane land <slug>` command still exists for manual lifecycle trigger.
- `dgov plan run` is now strictly fire-and-forget; monitor drives completion.
