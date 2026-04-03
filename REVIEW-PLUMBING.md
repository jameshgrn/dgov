# Plumbing Review

## Summary
The plumbing layer is functional but has accumulated dead code, duplicated utilities, and inconsistent patterns. The biggest concerns are: (1) several tmux governor-setup functions that are never called from any code path, (2) duplicate `_strip_ansi` implementations in status.py and done.py, and (3) stale guard code (`ensure_dag_tables`) that duplicates work already done at connection init.

## Findings by file

### persistence.py
- [LINE:~168] DEAD CODE: `ensure_dag_tables()` duplicates table creation already performed unconditionally inside `_get_db()` (lines ~310-313). This function is a no-op and can be removed.
- [LINE:~108-138] SIMPLIFY: `read_events()` has four repetitive branches for `(slug, limit)` combinations. Could be simplified with SQL parameterization or a query builder.
- [LINE:~250] MINOR: `_PANE_COLUMNS` frozenset is used only in `_insert_pane_dict`. Could be inlined if it has no other consumers.

### lifecycle.py
- [LINE:~145-153] INCOMPLETE: `_state_icon()` only handles 5 of 12 `PANE_STATES`. States like `failed`, `reviewed_pass`, `escalated`, `abandoned`, etc. silently fall through to `""`. Not dead code but inconsistent — callers expecting icons for terminal states get nothing.
- [LINE:~245] DUPLICATE: The `_PRE_MERGE_COMMIT_HOOK` string and `_install_worker_hooks()` are tightly coupled to git hook management but live in the lifecycle module instead of a hooks module. Not dead, but misplaced.
- [LINE:~450-500] COMPLEXITY: `_setup_and_launch_agent()` is a 180-line function handling env setup, hook triggers, prompt rewriting, done-signal setup, and three agent launch modes (interactive, send-keys, headless). Could be decomposed.

### waiter.py
- [LINE:13-18] REDUNDANT: Re-exports `_agent_still_running`, `_count_commits`, `_has_new_commits`, `_is_done`, `_resolve_strategy`, `_wrap_done_signal` from `dgov.done` with `# noqa: F401`. These are available directly from `dgov.done`; the re-export adds import surface without value.
- [LINE:35-46] SIMPLIFY: `_BLOCKED_PATTERNS` uses two case-sensitive regexes (`\bY/N\b`, `\[yes/no\]`) alongside case-insensitive variants that already match the same strings. The case-sensitive patterns are dead — they're strict subsets of the case-insensitive ones.
- [LINE:~180] SIMPLIFY: `wait_for_slugs()` duplicates much of `_poll_once` logic inline (pane lookup, alive check, stable_state tracking). Could delegate to `_poll_once` instead.

### done.py
- [LINE:~135-148] DUPLICATE: `_strip_ansi()` is defined identically in both `done.py` (imported via `dgov.status`) and `status.py` (defined locally at LINE:~435-448). The done.py version imports from status, but the regex pattern is duplicated as `_ANSI_RE` in both files. One canonical location would suffice.
- [LINE:~45-48] MINOR: `_CIRCUIT_BREAKER_LINES = 20` module constant is used only in `_circuit_breaker_fingerprint`. Could be inlined.

### status.py
- [LINE:~25-60] DUPLICATE: `_read_last_output_from_log()` duplicates `tail_worker_log()` (LINE:~460-480). Both read the last N lines from a log file with identical seek-and-truncate logic. `_read_last_output_from_log` is called from `list_worker_panes`; `tail_worker_log` is the public API. One should delegate to the other.
- [LINE:~435-448] DUPLICATE: `_ANSI_RE` and `_strip_ansi()` are defined here and also consumed (via import) in `done.py`. The regex is defined as a module-level constant in both files — if it changes in one, the other is stale.
- [LINE:~400-430] MINOR: `_read_progress_json()` handles two schema versions (v1 vs legacy). If v1 is the current standard, the legacy branch is likely dead code for new deployments.

### tmux.py
- [LINE:~200-210] DEAD CODE: `style_governor_pane()` is not called from any module in the codebase.
- [LINE:~215-225] DEAD CODE: `_style_pane()` is a private helper only called from `setup_governor_workspace()`. Both are dead if governor workspace setup is handled elsewhere.
- [LINE:~227-235] DEAD CODE: `_wait_for_shell()` is only called from `setup_governor_workspace()`.
- [LINE:~237-250] DEAD CODE: `_apply_governor_layout()` is only called from `setup_governor_workspace()`.
- [LINE:~252-262] DEAD CODE: `_write_lazygit_config()` is only called from `setup_governor_workspace()`.
- [LINE:~264-310] DEAD CODE: `setup_governor_workspace()` itself — not called from any CLI command or other module in the source tree.
- [LINE:~155-165] REDUNDANT: `style_worker_pane()` duplicates the styling logic now consolidated into `configure_worker_pane()`. `style_worker_pane` is never called; all callers use `configure_worker_pane`.
- [LINE:~320-330] MINOR: `start_logging()` and `stop_logging()` are standalone wrappers. `configure_worker_pane` already handles logging via `pipe-pane`, making these utility functions only useful for ad-hoc debugging.

### merger.py
- [LINE:~200-210] DEAD CODE: `commit_count` is computed in `_rebase_merge()` but never used in the return value or logging. The `rev-list --count` subprocess is wasted work.
- [LINE:~45] TYPO/BUG: Environment variable `DGOVPROTECTED_FILES` (missing underscore) should be `DGOV_PROTECTED_FILES`. Same typo on LINE:~530 and LINE:~560. This means the pre_merge and post_merge hooks receive a malformed env var name.
- [LINE:~60-70] SIMPLIFY: The stash-dirty-check pattern (`status --porcelain` → filter `??` → `stash push`) is repeated in `_plumbing_merge`, `_no_squash_merge`, and `_rebase_merge`. Could be extracted to a shared helper.
- [LINE:~250-270] SIMPLIFY: `_rebase_onto_head()` performs its own stash-less rebase, while `_rebase_merge()` has a more complete version with stash handling. The two functions have divergent behavior for the same operation.

## Cross-cutting concerns

1. **Duplicated ANSI stripping**: `_strip_ansi` and `_ANSI_RE` exist in both `status.py` and `done.py`. Should be canonicalized to one module (status.py is the natural home since it owns log reading).

2. **Duplicated log-tail reading**: `_read_last_output_from_log` (status.py) and `tail_worker_log` (status.py) do the same thing with slightly different signatures. The private function should delegate to the public one.

3. **Duplicated stash-then-merge pattern**: Three merge strategies in merger.py each implement their own stash-dirty / merge / stash-pop cycle. Extract `_with_stash(project_root, fn)` context manager.

4. **Inconsistent env var naming**: `DGOVPROTECTED_FILES` (missing underscore) vs `DGOV_PROJECT_ROOT`, `DGOV_SLUG`, etc. (correctly underscored). The typo propagates to hooks that read this variable.

5. **Module-level import patterns**: `waiter.py` re-exports 6 symbols from `done.py` with `noqa: F401`. Meanwhile, `done.py` imports `_strip_ansi` from `status.py`. This creates a circular-ish dependency chain (waiter → done → status) that could be flattened.

## Recommended deletions

| Function/Block | File | Reason |
|---|---|---|
| `ensure_dag_tables()` | persistence.py | No-op: `_get_db` already creates these tables |
| `style_governor_pane()` | tmux.py | Never called |
| `_style_pane()` | tmux.py | Only used by dead `setup_governor_workspace` |
| `_wait_for_shell()` | tmux.py | Only used by dead `setup_governor_workspace` |
| `_apply_governor_layout()` | tmux.py | Only used by dead `setup_governor_workspace` |
| `_write_lazygit_config()` | tmux.py | Only used by dead `setup_governor_workspace` |
| `setup_governor_workspace()` | tmux.py | Never called from any code path |
| `style_worker_pane()` | tmux.py | Superseded by `configure_worker_pane` |
| Re-export block (lines 13-18) | waiter.py | Symbols available directly from `done.py` |
| Case-sensitive `Y/N` and `[yes/no]` patterns | waiter.py | Subsets of case-insensitive patterns already present |
| `commit_count` computation | merger.py `_rebase_merge` | Result never used |
