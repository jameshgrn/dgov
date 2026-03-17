## Audit: Core State Management

### Critical Bugs
- **file:line**: `src/dgov/merger.py:771-787`
  **description**: `_restore_protected_files` amends the last commit in the worker worktree even when no protected files were changed, because it does not check whether `to_restore` is empty before running `git add` and `git commit --amend`. In the current code `to_restore` is checked and an early return occurs if empty, but if `git checkout` fails for some files (e.g., missing in base), the subsequent `git add` / `commit --amend` still run and may produce confusing history or errors.
  **severity**: P1
  **suggested fix**: After the checkout loop, recompute the set of successfully restored files (or track failures) and only run the `git add` / `commit --amend` block when there is at least one successfully restored file. Log and return early if none were restored.

- **file:line**: `src/dgov/lifecycle.py:768-779`
  **description**: `resume_worker_pane` updates the pane row by reading it via `_get_db` and re‑inserting with `_insert_pane_dict` without using the `update_pane_state` transition helper or a retry wrapper. A concurrent updater modifying the same row (e.g., state transition or metadata change) could race with this bulk replace and lose fields written by the other thread/process.
  **severity**: P1
  **suggested fix**: Use a targeted `UPDATE` that only modifies `pane_id`, `state`, and optionally `agent`, wrapped in `_retry_on_lock`, instead of re‑inserting the full row. Alternatively call a new helper in `persistence` that performs an atomic partial update with proper lock‑retry semantics.

### Logic Errors
- **file:line**: `src/dgov/merger.py:870-883`
  **description**: In `merge_worker_pane`, the auto‑rebase step calls `_rebase_onto_head` even when the worker branch has no commits ahead of `HEAD` (commit_count may be 0). Although `_rebase_onto_head` short‑circuits when already based on `HEAD`, it still performs subprocess calls for `merge-base` and `rev-parse`, which are redundant.
  **severity**: P3
  **suggested fix**: Before calling `_rebase_onto_head`, optionally skip the call if `branch_name` is already equal to the current branch or if a quick `git merge-base --is-ancestor HEAD branch_name` indicates it is already fast‑forwardable.

- **file:line**: `src/dgov/persistence.py:323-371`
  **description**: `_get_db` caches SQLite connections keyed by `(db_path, thread id)` but never evicts them outside of `_close_cached_connections`. Long‑running processes that spawn many threads over time could accumulate stale connections that are never reused.
  **severity**: P3
  **suggested fix**: Document this as intentional for the current lifetime model, or add a lightweight LRU/cleanup mechanism (e.g., on `_close_cached_connections` calls in tests or on process shutdown) to avoid unbounded growth in pathologically threaded environments.

### Inefficiencies
- **file:line**: `src/dgov/persistence.py:471-487`
  **description**: `list_panes_slim` performs `SELECT ... FROM panes` with no `ORDER BY` or index hint, which may result in full‑table scans as the number of panes grows. For large journals this can make status UIs slower than necessary.
  **severity**: P3
  **suggested fix**: Add an index on `created_at` and order by it (e.g., `ORDER BY created_at DESC`), or at least document the expected pane count. Consider adding a `LIMIT` for hot‑path UIs.

- **file:line**: `src/dgov/merger.py:457-517`
  **description**: `_lint_fix_merged_files` always runs `git diff --name-only` on the entire repo after running `ruff`, even when only a small subset of files are relevant. On large repos this can be an O(n) scan for each merge.
  **severity**: P3
  **suggested fix**: Limit the diff to the known `changed_files` (e.g., by passing them explicitly to `git diff`), or track whether `ruff` actually rewrote any of the input files via its exit code or output instead of diff‑scanning the repo.

### Dead Code
- **file:line**: `src/dgov/gitops.py:8-27`
  **description**: `_remove_worktree` is only used via the alias imported in `lifecycle.py` (`from dgov.gitops import _remove_worktree`) and only in `_full_cleanup`’s error path in `create_worker_pane`. There is no direct dead code here, but the function’s return value is ignored by callers, which makes its structured result effectively unused.
  **severity**: P3
  **suggested fix**: Either simplify `_remove_worktree` to return `None` and rely on logging, or have callers check the returned dict for `"success": False` and surface/log the error in a consistent way.

### Minor Issues
- **file:line**: `src/dgov/persistence.py:386-400`
  **description**: `_retry_on_lock` uses fixed backoff parameters (`_LOCK_RETRIES = 20`, `_LOCK_BACKOFF_S = 0.5`) leading to a potential 10‑second worst‑case delay. For non‑critical paths (like `emit_event`) this is reasonable, but for latency‑sensitive operations it may be excessive.
  **severity**: P3
  **suggested fix**: Allow callers to override retry/count or expose separate helpers for best‑effort vs critical operations so event logging can have a shorter backoff while state‑mutating calls can retain the more conservative defaults.

- **file:line**: `src/dgov/lifecycle.py:167-177`
  **description**: `_install_worker_hooks` unconditionally sets `core.hooksPath` inside the worktree, which can confuse developers if they expect standard hooks in `.git/hooks`. This is by design but subtle.
  **severity**: P3
  **suggested fix**: Add a short inline comment or log message when installing the hooks to make this behavior visible when debugging unexpected hook execution.

