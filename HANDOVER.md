# Handover: Dogfood Tests and Worktree Cleanup

**Date:** 2026-04-07T20:15:00Z
**Branch:** `main` @ `0baf72a`
**Context:** Added dogfood settlement timeout test, cleaned up abandoned worktree. Ready for next work.

---

## Completed

| Task | Status | Location | Notes |
|------|--------|----------|-------|
| Ledger Keyword Search | Done | `src/dgov/cli/ledger.py` | `dgov ledger list -q <query>` |
| Zombie Cleanup | Done | `src/dgov/cli/__init__.py` | `dgov cleanup` annihilates worktrees + states |
| Review Hooks | Done | `src/dgov/settlement.py` | User-defined shell hooks in `project.toml` |
| Dogfood Settlement Timeout | Done | `tests/test_settlement.py` | Slow test verifies `settlement_timeout` in TOML |
| Worktree Cleanup | Done | N/A | Removed abandoned `dgov/track_timing` worktree |
| Persistence Export | Done | `src/dgov/persistence/__init__.py` | Exported `cleanup_zombies` for CLI use |

## Blockers

- **Sentrux Path Exclusion**: Still blocked by upstream `sentrux` limitation where it looks for config in the target directory. Threshold remains at `0.75` to accommodate tests.

## Next Steps (Priority Order)

1. **Expand Review Hooks** — Add default hooks for binary detection and secret leakage to the project's own `project.toml`.
2. **Monitor Sentrux** — Watch for `sentrux` updates regarding `ignored_dirs` or multi-path checks.

## Files Modified

```
 M .dgov/project.toml
 M pyproject.toml
 M src/dgov/cli/__init__.py
 M src/dgov/cli/ledger.py
 M src/dgov/config.py
 M src/dgov/persistence/__init__.py
 M src/dgov/persistence/ledger.py
 M src/dgov/persistence/tasks.py
 M src/dgov/runner.py
 M src/dgov/settlement.py
 A tests/test_ledger.py
 A tests/test_review_hooks.py
 M tests/test_settlement.py
 M tests/test_runner.py
```

## Key Decisions

- **Review Hooks as Config**: Opted for a shell-based hook system in `project.toml` over hardcoding checks like secret detection into the kernel. This keeps the kernel generic.
- **Cleanup as Terminal Transition**: `dgov cleanup` marks tasks as `ABANDONED` rather than deleting them from the DB, preserving the event log integrity.
- **Review Sandbox Signature**: Added `project_root` to `review_sandbox` to allow it to load project-specific hooks.

## References

- Skill: `handover`, `dgov-bootstrap`
