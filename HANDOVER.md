# Handover: Ledger Search, Zombie Cleanup, and Review Hooks

**Date:** 2026-04-07T18:30:00Z
**Branch:** `main` @ `2ee7b49`
**Context:** Implemented keyword search for the ledger, a `cleanup` command for zombie tasks, and user-defined review hooks in `.dgov/project.toml`. Verified all with unit tests.

---

## Completed

| Task | Status | Location | Notes |
|------|--------|----------|-------|
| Ledger Keyword Search | Done | `src/dgov/cli/ledger.py` | `dgov ledger list -q <query>` |
| Zombie Cleanup | Done | `src/dgov/cli/__init__.py` | `dgov cleanup` annihilates worktrees + states |
| Review Hooks | Done | `src/dgov/settlement.py` | User-defined shell hooks in `project.toml` |
| Persistence Export | Done | `src/dgov/persistence/__init__.py` | Exported `cleanup_zombies` for CLI use |

## Blockers

- **Sentrux Path Exclusion**: Still blocked by upstream `sentrux` limitation where it looks for config in the target directory. Threshold remains at `0.75` to accommodate tests.

## Next Steps (Priority Order)

1. **Dogfood Settlement Timeout** — Add a slow test to verify `settlement_timeout` in `.dgov/project.toml` actually kills slow validations.
2. **Expand Review Hooks** — Add default hooks for binary detection and secret leakage to the project's own `project.toml`.
3. **Monitor Sentrux** — Watch for `sentrux` updates regarding `ignored_dirs` or multi-path checks.

## Files Modified

```
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
 M tests/test_runner.py
```

## Key Decisions

- **Review Hooks as Config**: Opted for a shell-based hook system in `project.toml` over hardcoding checks like secret detection into the kernel. This keeps the kernel generic.
- **Cleanup as Terminal Transition**: `dgov cleanup` marks tasks as `ABANDONED` rather than deleting them from the DB, preserving the event log integrity.
- **Review Sandbox Signature**: Added `project_root` to `review_sandbox` to allow it to load project-specific hooks.

## References

- Skill: `handover`, `dgov-bootstrap`
