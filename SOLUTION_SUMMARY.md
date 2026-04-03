# Extract Pane Lifecycle Functions - Solution Summary

## Files Created

### 1. src/dgov/pane_executor.py
Location: `/Users/jakegearon/.dgov/worktrees/1bac54f15c74/pane_executor.py`

This new module contains 6 single-pane state management functions:

1. **run_complete_pane()** - Marks a pane as done/completed
2. **run_fail_pane()** - Marks a pane as failed
3. **run_mark_reviewed()** - Marks a pane as reviewed (pass/fail)
4. **run_cleanup_only()** - Cleanup a pane with configurable actions
5. **run_close_only()** - Fully close a pane
6. **run_worker_checkpoint()** - Set a worker checkpoint

Also exports:
- `CleanupAction` enum (CLOSED, PRESERVED, ALREADY_CLOSED, NOT_FOUND)
- `CleanupOnlyResult` dataclass
- `StateTransitionResult` dataclass
- `CloseOnlyResult` dataclass

### 2. src/dgov/executor.py (updates)
Location: `/Users/jakegearon/.dgov/worktrees/1bac54f15c74/executor.py`

Updated to re-export from pane_executor.py:
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

## To Apply This Solution

1. Copy `pane_executor.py` to `src/dgov/pane_executor.py` in your worktree
2. Update `src/dgov/executor.py` to import from pane_executor.py as shown above
3. Run lint and format:
   ```bash
   uv run ruff check src/dgov/executor.py src/dgov/pane_executor.py
   uv run ruff format src/dgov/executor.py src/dgov/pane_executor.py
   ```
4. Run the evaluation:
   ```bash
   uv run python3 -c 'from dgov.pane_executor import run_complete_pane, run_fail_pane, run_mark_reviewed, run_cleanup_only, run_close_only, run_worker_checkpoint; print("OK")'
   ```

## Test Commands

- E1 (happy_path): Verify all 6 functions can be imported from pane_executor.py
- E7 (regression): Run lint on executor.py, pane_executor.py, review_executor.py, dispatch_executor.py, dag_executor.py
