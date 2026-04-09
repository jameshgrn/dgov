# Handover: dgov public-release UX and state cleanup

**Date:** 2026-04-09T14:47:30Z  
**Branch:** `main` @ `5e645cc0`  
**Context:** This session focused on making `dgov` feel more natural for public use. The core work was not kernel refactoring; it was execution-surface cleanup: add a governed one-off path, tighten scope enforcement, make `watch` current-run-first, separate transient runtime state from authored plans, and stop tracking generated artifacts.

---

## In Progress

| Task | Status | Location | Notes |
|------|--------|----------|-------|
| Public-release truthfulness pass | Partial | `/Users/jakegearon/projects/dgov/README.md`, `/Users/jakegearon/projects/dgov/docs/docs/pages` | Main install/clean/provider wording improved, but release docs still need final consistency pass |
| Runtime/state cleanup follow-through | Mostly done | `/Users/jakegearon/projects/dgov/.gitignore`, `/Users/jakegearon/projects/dgov/src/dgov/cli/fix.py`, `/Users/jakegearon/projects/dgov/src/dgov/cli/clean.py` | Generated fix plans now live under `.dgov/runtime/fix-plans`; generated artifacts no longer tracked |

## Blockers

- No blocker inside the repo right now.
- Release still depends on non-code work: PyPI/trusted publisher setup and final release notes/changelog.

## Next Steps (Priority Order)

1. Finish the remaining release messaging pass across `/Users/jakegearon/projects/dgov/README.md` and `/Users/jakegearon/projects/dgov/docs/docs/pages`.
2. Decide whether older authored archives under `/Users/jakegearon/projects/dgov/.dgov/plans/archive` should be retained or pruned.
3. Push `main` and let CI validate the recent cleanup/UX commits.
4. Prepare release notes/changelog for the first public release.

## Files Modified (Uncommitted)

```text
 M /Users/jakegearon/projects/dgov/HANDOVER.md
```

## Key Decisions

- `dgov fix` remains thin sugar over the normal plan pipeline; it does not introduce a second execution model.
- Unclaimed writes to `.dgov/` and `.sentrux/` are real scope violations now; infra paths are not exempt.
- Current-run observability is the default: `dgov watch` infers a single active plan when possible and otherwise live-tails from “now” instead of replaying repo history.
- Transient one-off plans belong to runtime state, not authored plan state. Generated fix plans now live under `/Users/jakegearon/projects/dgov/.dgov/runtime/fix-plans`, and `dgov clean` may delete them safely when inactive.
- Generated docs/build/runtime artifacts are not source of truth and should not be git-tracked. `.gitignore` now reflects that.

## Major Commits From This Session

- `4d1dc666` `Enforce infra path scope checks`
- `c29adbad` `Fix clean command docs`
- `75c0b32e` `Unify install docs`
- `f7dc4244` `Focus watch on active runs`
- `e9d6efcf` `Archive transient fix plans`
- `77bd0e42` `Clarify provider wording`
- `a8e4202a` `Separate transient fix plan state`
- `d24c3215` `Stop tracking generated artifacts`
- `5e645cc0` `Remove archived test plan debris`

## References

- `fix` CLI: `/Users/jakegearon/projects/dgov/src/dgov/cli/fix.py`
- `watch` CLI: `/Users/jakegearon/projects/dgov/src/dgov/cli/watch.py`
- `clean` CLI: `/Users/jakegearon/projects/dgov/src/dgov/cli/clean.py`
- settlement scope gate: `/Users/jakegearon/projects/dgov/src/dgov/settlement.py`
- fix tests: `/Users/jakegearon/projects/dgov/tests/test_cli_fix.py`
- clean tests: `/Users/jakegearon/projects/dgov/tests/test_cli_clean.py`
- watch tests: `/Users/jakegearon/projects/dgov/tests/test_cli.py`
