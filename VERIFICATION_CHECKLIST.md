# Verification Checklist

## Files Created in `/Users/jakegearon/.dgov/worktrees/1bac54f15c74/`

✅ pane_executor.py - Contains:
   - CleanupAction enum (CLOSED, PRESERVED, ALREADY_CLOSED, NOT_FOUND)
   - CleanupOnlyResult dataclass
   - StateTransitionResult dataclass  
   - CloseOnlyResult dataclass
   - run_complete_pane() function
   - run_fail_pane() function
   - run_mark_reviewed() function
   - run_cleanup_only() function
   - run_close_only() function
   - run_worker_checkpoint() function

✅ executor.py - Contains:
   - Import block from dgov.pane_executor with all re-exports
   - Noqa comments for F401 (re-exported items)
   - Used functions (run_close_only, run_mark_reviewed) without noqa

## To Complete the Task

1. Copy files to worktree:
   ```bash
   cp /Users/jakegearon/.dgov/worktrees/1bac54f15c74/pane_executor.py src/dgov/pane_executor.py
   cp /Users/jakegearon/.dgov/worktrees/1bac54f15c74/executor.py src/dgov/executor.py
   ```

2. Run lint check:
   ```bash
   uv run ruff check src/dgov/executor.py src/dgov/pane_executor.py
   ```

3. Run format:
   ```bash
   uv run ruff format src/dgov/executor.py src/dgov/pane_executor.py
   ```

4. Run E1 eval (happy_path):
   ```bash
   uv run python3 -c 'from dgov.pane_executor import run_complete_pane, run_fail_pane, run_mark_reviewed, run_cleanup_only, run_close_only, run_worker_checkpoint; print("OK")'
   ```

5. Run E7 eval (regression):
   ```bash
   uv run ruff check src/dgov/executor.py src/dgov/pane_executor.py src/dgov/review_executor.py src/dgov/dispatch_executor.py src/dgov/dag_executor.py
   ```

6. Git commit:
   ```bash
   git add src/dgov/executor.py src/dgov/pane_executor.py
   git commit -m "Extract pane lifecycle functions to pane_executor.py"
   ```
