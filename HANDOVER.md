# Handover: dgov Alpha Debut Stabilization (Phase 1)

**Date:** 2026-04-08T02:15:00Z
**Branch:** `main` @ `869dfab`
**Context:** Preparing dgov for alpha debut. Resolved worker 404 loops by implementing project-level agent resolution and overhauled documentation. Version bumped to `0.1.0a1`.

---

## Completed

| Task | Status | Location | Notes |
|------|--------|----------|-------|
| Version Bump | Done | `pyproject.toml`, `src/dgov/__init__.py` | To `0.1.0a1` |
| README Update | Done | `README.md` | Fixed CLI examples |
| Docs Overhaul | Done | `CLAUDE.md`, `GEMINI.md` | Modern toolchain & Governor role defined |
| Agent Resolution | Done | `src/dgov/config.py`, `src/dgov/runner.py` | Runners now re-resolve agent short-names via `project.toml` mapping |
| Status Visibility | Done | `src/dgov/cli/__init__.py` | Removed 10-task limit in `dgov status` |
| Auditor Prompt | Done | `src/dgov/worker.py` | Added Settlement Layer awareness and scope guardrails |

## In Progress

| Task | Status | Location | Notes |
|------|--------|----------|-------|
| Alpha Debut Plan | Partial | `.dgov/plans/alpha-debut/` | Quality fixes and hooks abandoned due to kernel crash |
| Kernel Stability | Blocked | `src/dgov/runner.py` | Occasional silent crashes/database locks during `--continue` |

## Blockers

- **Runner Deadlocks**: Occasional synchronous contention in `_executor` during settlement validation.
- **Database Connection Contention**: `dgov watch` polling might conflict with runner writes in high-volume runs.

## Next Steps (Priority Order)

1. **Investigate Runner Contention**: Profile `runner.py` to see why it hangs/crashes during large plan execution.
2. **Re-run Alpha Debut Plan**: Pick up the abandoned `quality/fix.fix-warnings` and `quality/hooks.expand-hooks` tasks.
3. **Dogfooding**: Perform a full end-to-end "dogfood" run of a complex plan.
4. **watch.log / run.log Cleanup**: These are untracked and should be reviewed/archived.

## Files Modified (Uncommitted)

```
 M .dgov/plans/deployed.jsonl
 M .dgov/project.toml
 M .dgov/runs.log
 M .sentrux/baseline.json
 M CLAUDE.md
 M GEMINI.md
 M src/dgov/cli/__init__.py
 M src/dgov/config.py
 M src/dgov/kernel.py
 M src/dgov/runner.py
 M src/dgov/settlement.py
 M src/dgov/worker.py
 M uv.lock
```

## Key Decisions

- **Governor Role**: Formally adopted the principle that the Governor never edits `src/` directly (orchestration only).
- **Project Agents**: Mapping `kimi -> full Fireworks router path` now lives in `.dgov/project.toml` instead of plans.
- **Settlement Rejection**: Any file touch outside `files.edit` claim is an automatic rejection.
