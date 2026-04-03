# Extraction Summary: Pane Lifecycle Functions

## Changes Made

### 1. Created `src/dgov/pane_executor.py`
New module containing 6 single-pane state management functions:

- `run_complete_pane()` - Mark a pane as done
- `run_fail_pane()` - Mark a pane as failed  
- `run_mark_reviewed()` - Transition pane to reviewed_pass or reviewed_fail
- `run_cleanup_only()` - Run canonical cleanup policy for terminal lifecycle outcomes
- `run_close_only()` - Close a pane and reclaim resources
- `run_worker_checkpoint()` - Record a worker checkpoint

Plus supporting types:
- `CleanupAction` enum (PRESERVE, CLOSE, CLOSED)
- `CleanupOnlyResult` dataclass
- `StateTransitionResult` dataclass
- `_CLEANUP_POLICY` dict

### 2. Modified `src/dgov/executor.py`
- Removed the 6 functions and their supporting types
- Removed unused imports (`StrEnum`, `sqlite3`)
- Added re-export imports from `dgov.pane_executor`:
  ```python
  from dgov.pane_executor import (
      CleanupAction,
      CleanupOnlyResult,
      StateTransitionResult,
      run_cleanup_only,
      run_close_only,
      run_complete_pane,
      run_fail_pane,
      run_mark_reviewed,
      run_worker_checkpoint,
  )
  ```

## Backward Compatibility
The re-exports in `executor.py` maintain backward compatibility - existing code that imports from `dgov.executor` will continue to work.

## Verification Commands (to be run manually)

```bash
cd /Users/jakegearon/projects/dgov

# 1. Lint check
uv run ruff check src/dgov/executor.py src/dgov/pane_executor.py src/dgov/review_executor.py src/dgov/dispatch_executor.py src/dgov/dag_executor.py

# 2. Format files
uv run ruff format src/dgov/executor.py src/dgov/pane_executor.py

# 3. Verify exports
uv run python3 -c 'from dgov.pane_executor import run_complete_pane, run_fail_pane, run_mark_reviewed, run_cleanup_only, run_close_only, run_worker_checkpoint; print("OK")'

# 4. Git add and commit
git add src/dgov/executor.py src/dgov/pane_executor.py
git commit -m "Extract pane lifecycle functions to pane_executor.py"
```

## Test Files Referenced
Tests for these functions exist in:
- `tests/test_executor.py` - imports from `dgov.executor` (still works via re-exports)
- `tests/test_lifecycle.py`
- `tests/test_dgov_panes.py`
- `tests/test_dgov_state.py`
- `tests/test_persistence_pane.py`
