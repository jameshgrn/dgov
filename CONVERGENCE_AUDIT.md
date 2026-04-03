# Convergence Audit Report — dgov codebase

## Executive Summary

The dgov codebase shows **moderate convergence drift** — mostly due to type/enum duplication across modules. The architectural patterns (event-driven design, pure kernel, file claims) are well-adopted. The main issues are:

1. **Type duplication** — WorkerPhase/WorkerObservation in two modules
2. **PaneState drift** — stub version in types.py vs canonical in schema.py
3. **Signal detection fragmentation** — logic split across types.py, observation.py, done.py
4. **Import inconsistency** — different modules import same types from different sources

---

## Convergence Issues

### Issue 1: WorkerPhase/WorkerObservation Duplication [HIGH]

**Problem:**
- `types.py` defines `WorkerPhase` (13 members) and `WorkerObservation` (with exit_code, classification, reason)
- `observation.py` defines the same classes with different fields (alive, done, duration_s, summary)
- `kernel.py` imports from `types.py` but `monitor.py` uses `observation.py`

**Canonical Rule Violated:**
> "Single source of truth for worker state classification" — CLAUDE.md, observation.py docstring

**Impact:**
- Inconsistent worker state interpretation
- Risk of divergence when adding new phases
- Confusion about which to use for new code

**Fix:**
- Consolidate both into `observation.py` (it has richer classification)
- `types.py` should re-export for backward compatibility
- Update all imports to use `observation.py` as primary source

---

### Issue 2: PaneState Enum Drift [MEDIUM]

**Problem:**
- `types.py` PaneState has only 7 states (ACTIVE, FAILED, MERGED, CLOSED, SUPERSEDED, TIMED_OUT) — MISSING done, reviewed_pass, reviewed_fail, abandoned
- `schema.py` has the complete 11-state enum with VALID_TRANSITIONS table
- Kernel uses PaneState from persistence/schema, which is correct

**Impact:**
- Incomplete type hints in some paths
- Risk of runtime errors if types.py version is used

**Fix:**
- Delete stub from `types.py` (only re-export from schema.py)
- Keep all state logic in `persistence/schema.py` as source of truth

---

### Issue 3: Signal Detection Fragmentation [MEDIUM]

**Problem:**
- `types.py` has `_SIGNAL_PATTERNS`, `_match_signal()`, `extract_summary_from_log()`
- `observation.py` has nearly identical `match_signal()`, `extract_summary_from_log()`
- `done.py` is a stub with minimal `_strip_ansi()` and stub `_has_new_commits()`

**Canonical Design:**
Signal detection should live in `observation.py` (the unified observation provider) or `done.py` (completion detection), not scattered.

**Impact:**
- Duplicated regex patterns may diverge
- Done detection logic scattered

**Fix:**
- Consolidate signal extraction into `observation.py`
- Make `done.py` a thin re-export layer
- Keep `types.py` minimal — only pure types with no logic

---

### Issue 4: Import Inconsistency [LOW-MEDIUM]

**Problem:**
```python
# Various patterns seen:
from dgov.persistence import PaneState              # canonical
from dgov.persistence.schema import PaneState       # implementation detail
from dgov.types import WorkerPhase                  # should use observation
from dgov.observation import WorkerPhase            # correct
```

**Impact:**
- Makes refactoring harder
- Creates invisible dependencies
- Violates "single source of truth"

**Fix:**
- Update all imports to use canonical sources:
  - `persistence` (re-exported) for PaneState
  - `observation` for WorkerPhase and WorkerObservation

---

## What IS Converged (Good)

1. ✅ **Event-driven architecture** — EventDagRunner, pipe-based signaling
2. ✅ **Pure kernel** — No I/O, no imports of executor/lifecycle
3. ✅ **File claims** — Explicit file specs in DAG tasks
4. ✅ **Worktree isolation** — Clean _git_env(), branch advancement check
5. ✅ **Commit proof verification** — runner.py verifies branch advanced
6. ✅ **ObservationProvider** — Centralized classification logic
7. ✅ **Dispatch loop** — Preflight → spawn → track with spans

---

## Fix Priority

| Priority | Issue | Effort | Files Affected |
|----------|-------|--------|----------------|
| P1 | WorkerPhase consolidation | Low | types.py, observation.py, kernel.py, monitor.py |
| P1 | PaneState cleanup | Low | types.py |
| P2 | Signal extraction merge | Medium | types.py, observation.py |
| P2 | Import normalization | Medium | All consumers |

---

## Suggested Plan

```
Step 1: types.py cleanup
  - Remove duplicate WorkerPhase and WorkerObservation
  - Remove duplicate signal extraction logic
  - Keep only minimal stubs that re-export from canonical sources

Step 2: observation.py as canonical source
  - Ensure it has all fields needed by kernel and monitor
  - Add __all__ for explicit exports

Step 3: Import fixes
  - Update kernel.py to import from observation
  - Update monitor.py (already correct)
  - Audit other modules

Step 4: done.py consolidation
  - Either merge into observation or make it a thin wrapper
```
