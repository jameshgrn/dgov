# LT-GOV Routing Audit

**Scope:** Adversarial review of `DGOV_PROJECT_ROOT` auto-correct blocks in CLI commands.
**Files reviewed:** `src/dgov/cli/pane.py`, `src/dgov/cli/admin.py`, `src/dgov/cli/dag_cmd.py`
**Date:** 2026-03-17

## P0 (will crash or corrupt)

None. The auto-correct pattern is defensive — missing it causes wrong-state reads, not crashes.
However, if a worktree accidentally contains its own `.dgov/state.db` (e.g., git-tracked by mistake),
commands without the fix will read/write the **wrong database silently**. This is a corruption vector
but requires a precondition (accidental state.db in worktree).

## P1 (incorrect behavior — commands that need fix but don't have it)

**20 commands in `pane.py` take `-r/--project-root` but are MISSING the auto-correct.**
Only 4 of 24 commands have it: `pane_create`, `pane_close`, `pane_merge`, `pane_wait`.

### Commands that touch state.db and are missing the fix (directly asked about):

| Command | What it does | Failure mode |
|---------|-------------|--------------|
| `pane_review` | Reads worktree state via `review_worker_pane` | Wrong worktree path → "pane not found" or reviews wrong worktree |
| `pane_resume` | Re-launches agent in existing worktree | Wrong state.db → "pane not found" |
| `pane_output` | Captures/tails worker output | Wrong state.db → can't find pane metadata |
| `pane_list` | Lists all panes from state.db | Reads empty/wrong state.db → shows no panes |
| `pane_merge_request` | Enqueues merge, writes to state.db | Writes to wrong state.db → queue entry lost or corrupt |
| `pane_retry_or_escalate` | Retry logic reads pane state | Wrong state.db → "pane not found" |
| `pane_retry` | Retry a failed pane | Wrong state.db → "pane not found" |

Note: `merge_queue_process` does not exist in the codebase. Not an issue.

### Other commands missing the fix that interact with state/paths:

| Command | Issue |
|---------|-------|
| `pane_land` | Calls `review_worker_pane` + `merge_worker_pane` — both need correct root |
| `pane_batch` | Calls `create_worker_pane` — creates worktrees in wrong repo |
| `pane_wait_all` | Calls `list_worker_panes` + `wait_all_worker_panes` — wrong state |
| `pane_merge_all` | Calls `merge_worker_pane` — wrong state.db, wrong merge target |
| `pane_prune` | Reads state.db for stale cleanup |
| `pane_capture` | Reads state.db for pane metadata |
| `pane_diff` | Reads state.db + git worktree paths |
| `pane_escalate` | Reads state.db for pane state |
| `pane_logs` | Computes log path from `session_root` (which is also uncorrected) |
| `pane_message` | Reads state.db for pane lookup |

### Admin commands also missing the fix:

| Command | File | Issue |
|---------|------|-------|
| `preflight` | `admin.py` | Runs checks against wrong project root |
| `status` | `admin.py` | Reads wrong state.db |
| `blame` | `admin.py` | Reads wrong state.db for attribution |
| `stats` | `admin.py` | Reads wrong state.db |
| `dashboard` | `admin.py` | Launches dashboard reading wrong state |
| `doctor` | `admin.py` | Diagnostics on wrong project (misleading output) |
| `rebase` | `admin.py` | Rebases wrong worktree |
| `list_agents` | `admin.py` | Loads registry from wrong root (minor — only if agents differ) |

### DAG commands:

| Command | File | Issue |
|---------|------|-------|
| `dag run` | `dag_cmd.py` | No `-r` option at all — inherits from env but no auto-correct |
| `dag merge` | `dag_cmd.py` | Same — no `-r` option, no auto-correct |

The DAG commands don't take `-r` explicitly, but they call into `run_dag`/`merge_dag` which
internally use `project_root`. If invoked from a worktree with `DGOV_PROJECT_ROOT` set, the env
var may or may not propagate correctly depending on how the DAG module resolves it.

## P2 (edge cases)

### 1. False positive: project root legitimately contains `.dgov/worktrees/`

**Heuristic:** `_os.path.abspath(project_root).contains("/.dgov/worktrees/")`

If the project itself lives at `/home/user/.dgov/worktrees/myproject/`, then any subpath
(`/home/user/.dgov/worktrees/myproject/src`) will also match. The auto-correct would
**incorrectly rewrite** `project_root` to `DGOV_PROJECT_ROOT`, which would be the *same*
path (since the env var would also point here). This is **benign** — the correction is
idempotent when both point to the same place. But if `DGOV_PROJECT_ROOT` points elsewhere,
this would be a **silent misdirection**.

**Risk:** Low. Unlikely that someone names their project directory `.dgov/worktrees/`.
**Mitigation:** Could use `os.path.realpath` and check for git worktree metadata instead.

### 2. Nonexistent `DGOV_PROJECT_ROOT` directory

**Scenario:** `DGOV_PROJECT_ROOT=/nonexistent/path` — no validation that the directory exists.

- Commands that only read paths (like `list`) will fail with "state.db not found" — confusing but not corrupt.
- Commands that write (like `pane_create`) will **create directories** at the nonexistent path, polluting the filesystem with a bogus `.dgov/` tree.

**Risk:** Medium. Typo in env var → silent directory creation.
**Mitigation:** Validate `os.path.isdir(DGOV_PROJECT_ROOT)` early, or at minimum check `os.path.exists()`.

### 3. `session_root` not corrected when `project_root` is

**Current behavior:** The auto-correct fixes `project_root` but leaves `session_root` untouched.

```python
_dgov_pr = _os.environ.get("DGOV_PROJECT_ROOT")
if _dgov_pr and "/.dgov/worktrees/" in _os.path.abspath(project_root):
    project_root = _dgov_pr
# session_root is NOT updated
```

If `session_root` was explicitly set (not None), it stays pointing at the worktree path.
Most commands compute `session_root_abs = os.path.abspath(session_root or project_root)`,
so they use the corrected `project_root` as fallback. But if `session_root` was passed
explicitly, it diverges.

**Risk:** Low. `--session-root` is rarely used; most callers rely on the default.
**Mitigation:** Also check and correct `session_root` when it contains `.dgov/worktrees/`.

### 4. `pane_land` is doubly broken

`pane_land` calls `review_worker_pane` then `merge_worker_pane` — both need correct state.
It has no auto-correct, AND it doesn't set `session_root` before calling these functions.
This is a compound bug: even if you fix the auto-correct, you need to pass `session_root`
through correctly.

### 5. Inconsistent auto-correct placement

The auto-correct is placed at the **top** of the function body, after imports. But `pane_batch`
does `project_root = os.path.abspath(project_root)` early (line ~175) — if the auto-correct
were added, it should come **before** this `abspath` call to be effective.

### 6. DAG commands lack `-r` entirely

`dag run` and `dag merge` don't accept `--project-root`. They presumably resolve from cwd or
env internally, but there's no explicit option. If someone runs `dgov dag run plan.toml -r /path`,
it will error with "no such option". This is a usability issue for LT-GOV orchestration.

## Recommendations

1. **Extract the auto-correct into a helper** and apply it consistently:
   ```python
   def _autocorrect_project_root(project_root: str, session_root: str | None) -> tuple[str, str | None]:
       dgov_pr = os.environ.get("DGOV_PROJECT_ROOT")
       if dgov_pr and "/.dgov/worktrees/" in os.path.abspath(project_root):
           project_root = dgov_pr
       if dgov_pr and session_root and "/.dgov/worktrees/" in os.path.abspath(session_root):
           session_root = dgov_pr
       return project_root, session_root
   ```
   Call this at the top of every command that takes `-r`.

2. **Validate `DGOV_PROJECT_ROOT`** — at minimum, check `os.path.isdir()` and warn/fail early.

3. **Consider a Click callback** — use `@click.option(..., callback=autocorrect_callback)` to
   apply the fix centrally rather than duplicating it in every command body.

4. **Add `-r` to DAG commands** for consistency with the rest of the CLI.

5. **Add integration tests** that exercise commands from inside a worktree with `DGOV_PROJECT_ROOT`
   set, verifying they read the correct state.db.
