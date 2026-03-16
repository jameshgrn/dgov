# dgov Speed & Efficiency Audit

Audited files: `lifecycle.py`, `status.py`, `done.py`, `waiter.py`, `tmux.py`,
`backend.py`, `persistence.py`, `dashboard.py`, `inspection.py`, `gitops.py`,
`agents.py`, `strategy.py`, `merger.py`, `recovery.py`.

---

## 1. Subprocess Calls per Worker Lifecycle

Every `subprocess.run()` and tmux `_run()` is a `fork+exec`. On macOS,
each costs ~15-30ms. The counts below are **minimum** forks (happy path).

### CREATE (`create_worker_pane`) — 14 forks + 250ms sleep

| # | Call | File:Line | Cost |
|---|------|-----------|------|
| 1 | `git worktree prune` | `lifecycle.py:55` | ~30ms |
| 2 | `git rev-parse --verify <branch>` | `lifecycle.py:67-71` | ~20ms |
| 3 | `git worktree add` | `lifecycle.py:74` or `:81` | ~50ms |
| 4 | `git rev-parse HEAD` (base_sha) | `lifecycle.py:354-358` | ~20ms |
| 5 | `tmux list-panes -a` (bulk_info for concurrency guard) | `status.py:138` | ~20ms |
| 6 | `tmux set-option` (pane-border-status) | `tmux.py:174` | ~15ms |
| 7 | `tmux set-option` (pane-border-format) | `tmux.py:175-183` | ~15ms |
| 8 | `tmux new-window -d` | `tmux.py:54-63` | ~20ms |
| 9 | **`time.sleep(0.25)`** | `lifecycle.py:398` | **250ms** |
| 10 | `tmux` compound (7 ops via `;`) | `tmux.py:263-273` | ~20ms |
| 11 | `tmux pipe-pane` (logging) | `tmux.py:369` | ~15ms |
| 12 | `tmux send-keys` (env export) | `tmux.py:79` | ~15ms |
| 13 | Hook subprocess | `lifecycle.py:120-127` | ~50-200ms |
| 14 | `tmux send-keys` (launch cmd) | `tmux.py:79` or `:91` | ~15ms |

**Total: ~555-705ms** (250ms is pure sleep).

For **send-keys transport** agents (cline, crush), add:
- `time.sleep(send_keys_ready_delay_ms)` — up to **2500ms** for cline
- 4-5 extra tmux `_run()` calls (set-buffer, paste-buffer, send-keys Enter, delete-buffer)

**Total for cline: ~3.1s** of which 2.75s is sleep.

### WAIT (`wait_worker_pane` / `_poll_once` / `_is_done`) — 1-3 forks per tick

Default poll interval: 3s. Per tick:

| Strategy | Forks/tick | Calls |
|----------|-----------|-------|
| **exit** (default) | 1 | `Path.exists()` ×2, `tmux display-message` (is_alive) |
| **signal** | 3 | + `git log` (commit check), + `tmux capture-pane` (stabilization) |
| **commit** | 2 | + `git log` (commit check) |
| **stable** | 2 | + `tmux capture-pane`, `tmux display-message` (current_command) |

At 3s polling over a 10-minute task: ~200 ticks × 1 fork = **200 forks** (exit strategy).

### REVIEW (`review_worker_pane`) — 10 forks

| # | Call | File:Line |
|---|------|-----------|
| 1 | `git diff --stat base..HEAD` | `inspection.py:44-48` |
| 2 | `git diff --name-only base..HEAD` | `inspection.py:52-56` |
| 3 | `git log --oneline base..HEAD` | `inspection.py:63-67` |
| 4 | `git status --porcelain` | `inspection.py:72-76` |
| 5 | `git log base..HEAD --oneline` (_compute_freshness) | `status.py:75-81` |
| 6 | `git diff --name-only base..HEAD` (_compute_freshness) | `status.py:86-92` |
| 7 | `git diff --name-only base..HEAD` (_compute_freshness, worker) | `status.py:97-103` |
| 8-10 | SQLite queries + event emit | `persistence.py` |

**Calls 1+5 are the same query run twice** (once in review, once in freshness).
**Calls 2+6 are the same query run twice.**

With `full=True`, add 1 more fork for the full diff.

### MERGE (`merge_worker_pane`) — 25-35 forks

Breakdown of the squash-merge happy path:

| Phase | Forks | Key calls |
|-------|-------|-----------|
| Auto-commit worktree | 3 | status, add, commit |
| Pre-merge hook or restore | 1-4 | hook OR diff+checkout+add+amend |
| Diff stat capture | 2 | diff --stat, diff --name-only |
| Plumbing merge | 8-10 | rev-parse ×3, merge-tree, commit-tree, symbolic-ref, status, update-ref, reset --hard, optional stash×2 |
| Post-merge cleanup | 6 | kill-pane, is_alive, worktree remove, branch -d, worktree prune, rev-parse HEAD |
| Post-merge hook or lint | 1-5 | hook OR ruff check + ruff format + diff + add + amend |

### CLOSE (`close_worker_pane`) — 6-9 forks

| # | Call | File:Line |
|---|------|-----------|
| 1 | `tmux kill-pane` | `lifecycle.py:482` via backend |
| 2 | `tmux display-message` (is_alive) | `lifecycle.py:483` |
| 3 | Optional: `tmux kill-pane` retry | `lifecycle.py:485` |
| 4 | Optional: `git status --porcelain` | `lifecycle.py:495-496` |
| 5 | `git worktree remove --force` | `lifecycle.py:507-509` |
| 6 | `git branch -d` | `lifecycle.py:512-516` |
| 7 | `git worktree prune` | `lifecycle.py:526-529` |
| 8 | `tmux select-pane -T` (title update via update_pane_state) | `persistence.py:449` |

---

## 2. SQLite: Connection Patterns, N+1 Queries, Lock Contention

### Connection pattern
- Per `(db_path, thread_id)` caching with `threading.Lock` (`persistence.py:22-23`)
- WAL mode + `busy_timeout=5000` (`persistence.py:249-250`)
- `_retry_on_lock`: 5 retries at `0.2s * (attempt+1)` backoff (`persistence.py:280-290`)
- Each write wraps in `_retry_on_lock` — good

### N+1 queries

| Location | Pattern | Cost |
|----------|---------|------|
| `list_worker_panes` `status.py:184-187` | 1 query for all panes, then per-pane `get_pane()` if `_is_done` triggers state update | +1 SELECT per done pane |
| `update_pane_state` `persistence.py:437-451` | After UPDATE, reads back pane (`get_pane`) to update tmux title | +1 SELECT + 1 tmux fork per state change |
| `prune_stale_panes` `status.py:264-307` | `all_panes()` called twice (once for pass 1, once for pass 2 after re-read at line 293) | Double full-table scan |
| `resume_worker_pane` `lifecycle.py:683-693` | `get_pane` at start, then raw `SELECT * + _insert_pane_dict` at end | 2 SELECTs when 1 UPDATE suffices |
| `set_pane_metadata` `persistence.py:461-473` | SELECT + full row rebuild + INSERT OR REPLACE to update 1 field | Should be JSON patch or column UPDATE |

### Lock contention risks
- Dashboard background thread holds a cached connection open → CLI commands on same thread ID
  get the same connection (fine), but commits from CLI compete with dashboard reads
- `emit_event` and `update_pane_state` each call `conn.commit()` — frequent small txns
- **Proposed**: batch state-update + event-emit into a single transaction

---

## 3. Bootstrap Latency: Critical Path

Sequential timeline from `create_worker_pane()` entry to agent receiving its prompt:

```
0ms     ensure_dgov_gitignored          ~1ms   (file I/O)
1ms     load_registry                   ~2ms   (reads 2 TOML files)
3ms     git rev-parse HEAD              ~20ms  (fork)
23ms    git worktree prune              ~30ms  (fork)          [REDUNDANT if recent]
53ms    git rev-parse --verify          ~20ms  (fork)
73ms    git worktree add                ~50ms  (fork)
123ms   _count_active_agent_workers     ~22ms  (SQLite + tmux bulk_info fork)
145ms   setup_pane_borders              ~30ms  (2 tmux forks)  [REDUNDANT after first]
175ms   tmux new-window -d              ~20ms  (fork)
195ms   >>> time.sleep(0.25) <<<        250ms  *** HARDCODED DELAY ***
445ms   configure_worker_pane           ~20ms  (1 compound tmux fork)
465ms   start_logging (pipe-pane)       ~15ms  (fork)
480ms   send_shell_command (env)        ~15ms  (fork)
495ms   _trigger_hook                   ~50ms  (fork)
545ms   send_shell_command (launch)     ~15ms  (fork)
560ms   AGENT RECEIVES PROMPT
```

**Critical path: ~560ms, of which 250ms (45%) is a hardcoded sleep.**

### Bottlenecks
1. **`time.sleep(0.25)`** at `lifecycle.py:398` — waiting for shell to be "ready" after
   `tmux new-window`. This is a cargo-cult delay. tmux synchronously creates the pane;
   the shell is ready when `new-window` returns.
2. **`setup_pane_borders`** called every create (`lifecycle.py:394`) — idempotent but
   costs 2 forks. Should be called once per session.
3. **`git worktree prune`** at `lifecycle.py:55` — called unconditionally before every
   worktree add. Only needed if a previous close failed.
4. **`load_registry`** reads TOML files on every create — could cache with mtime check.

---

## 4. Wait Loop: Polling Frequency and Per-Tick Work

### Default: `poll=3` seconds (`waiter.py:233`)

Per tick in `_poll_once` → `_is_done`:

| Check | Condition | Cost |
|-------|-----------|------|
| Done file exists | Always | `Path.exists()` — ~0.01ms |
| Exit file exists | Always | `Path.exists()` — ~0.01ms |
| Commit check | strategy not "exit"/"stable" | `git log` fork ~20ms |
| If commits + running | commit found | `tmux display-message` fork ~15ms + `git rev-list --count` fork ~20ms |
| Pane alive | Always (if pane_id) | `tmux display-message` fork ~15ms |
| Output stabilization | strategy "stable" or "signal" | `tmux capture-pane` fork ~15ms |
| Agent still running | stabilized | `tmux display-message` fork ~15ms |
| Blocked detection | output captured | regex scan — ~0.1ms |

**Unnecessary work per tick:**
- `_is_done` calls `get_backend().is_alive(pane_id)` which forks `tmux display-message`.
  But `list_worker_panes` already called `bulk_info()` which got all pane info in 1 fork.
  The wait loop doesn't use `bulk_info` — it checks each pane individually.
- For `wait_all_worker_panes`, N panes = N individual `is_alive` forks per tick when
  1 `bulk_info` call would suffice for all of them.

### Blocked detection regex (`waiter.py:31-41`)
- 8 pre-compiled patterns scanned against last 10 lines
- Patterns are simple (no nested quantifiers, no backtracking risk)
- Cost: negligible (~0.1ms)

---

## 5. Dashboard Hot Path: I/O per Refresh

`fetch_panes()` at `dashboard.py:176-219` runs every `refresh_interval` (default 1.0s):

| Step | I/O | Cost |
|------|-----|------|
| `list_panes_slim()` | 1 SQLite SELECT | ~1ms |
| `bulk_info()` | 1 tmux fork | ~20ms |
| Per active pane: `_is_done()` | 2 `Path.exists()` + 1 tmux fork (is_alive) | ~15ms each |
| Per pane: `_read_last_output_from_log()` | **Reads ENTIRE log file line by line** | **O(n), potentially seconds** |
| Per pane: progress JSON read | 1 `read_text()` per file | ~1ms each |
| Per pane without activity: `tail_worker_log()` | Seek-from-end read | ~2ms |
| `_get_branch()` | 1 git fork | ~20ms |
| Curses render: `tail_worker_log()` for selected pane | Seek-from-end read | ~2ms |

### Critical finding: `_read_last_output_from_log()` at `status.py:32-45`

```python
def _read_last_output_from_log(session_root: str, slug: str) -> str:
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        lines: deque[str] = deque(maxlen=3)
        for line in handle:                    # <-- reads EVERY line
            lines.append(_strip_ansi(line.rstrip("\r\n")))
```

This reads the **entire log file** to get the last 3 lines. For a worker that has
been running for 30 minutes producing terminal output, the log can be **10-50MB**.
This runs **per pane, per refresh** (every 1s).

**This is the single biggest performance problem in dgov.**

Compare with `tail_worker_log()` at `status.py:356-367` which correctly seeks from
the end and reads only `lines * 512` bytes. The fix is trivial: replace
`_read_last_output_from_log` with a seek-from-end approach.

### `_strip_ansi` defined twice

- `status.py:25-29` — simple 3-pattern regex
- `status.py:329-341` — comprehensive 6-pattern regex (handles charset selection, keypad
  modes, control chars)

The simple version at line 25 is what `_read_last_output_from_log` uses. The
comprehensive one at line 329 is what `tail_worker_log` uses. The simple version
will leave artifacts for charset/keypad escapes.

---

## 6. Git Ops: Subprocess Calls per Review/Merge

### review_worker_pane — 10 forks (4 redundant)

```
inspection.py:44   git diff --stat base..HEAD       ─┐
inspection.py:52   git diff --name-only base..HEAD   │ same data as freshness calls below
inspection.py:63   git log --oneline base..HEAD      │
inspection.py:72   git status --porcelain            │
                                                     │
status.py:75       git log base..HEAD --oneline     ─┤ DUPLICATE of :63
status.py:86       git diff --name-only base..HEAD  ─┤ DUPLICATE of :52
status.py:97       git -C wt diff --name-only       ─┘ unique (worker branch)
```

**4 of 10 calls are redundant.** `_compute_freshness` re-runs git log and git diff
that `review_worker_pane` already has.

**Fix**: Pass the already-computed data into `_compute_freshness` or compute freshness
inside review using the data already fetched.

### merge_worker_pane — 25-35 forks

The merge path is inherently sequential (each git op depends on the previous), but:

1. **Pre-merge diff stat** (`merger.py:645-659`): runs `git diff --stat` and
   `git diff --name-only` on the worktree — then the merge runs `git merge-tree`
   which implicitly computes the same diff. These 2 forks are wasted if merge fails.

2. **`git worktree prune`** called in `_full_cleanup` (`lifecycle.py:526-529`) AND in
   `_remove_worktree` (`gitops.py:19-25`). If close/cleanup calls `_remove_worktree`,
   prune runs twice.

3. **`git worktree prune`** at `lifecycle.py:55` — also runs before every create.
   In the worst case, a create-then-merge-then-close cycle runs worktree prune **3 times**.

### _remove_worktree — 3 forks (always)

```
gitops.py:10   git worktree remove --force
gitops.py:17   git branch -D
gitops.py:19   git worktree prune
```

Called by `_full_cleanup` which ALSO runs `git worktree remove`, `git branch -d`,
and `git worktree prune` at `lifecycle.py:507-529` — meaning the caller does the
same work. However, `_full_cleanup` does `-d` (safe delete) while `_remove_worktree`
does `-D` (force delete), so they're different paths and won't both be reached.

---

## 7. Log Tail: Regex Backtracking Risk, File Seeking Efficiency

### `_read_last_output_from_log` (status.py:32-45) — BAD

- **O(n)** where n = total log file size
- Reads entire file line-by-line, applies ANSI stripping to every line
- Uses `deque(maxlen=3)` to keep last 3 — correct logic but wrong I/O pattern
- Called per pane per dashboard refresh (1s interval)
- **Estimated cost**: 50-500ms per pane for 10-50MB logs

### `tail_worker_log` (status.py:343-377) — GOOD

- Seeks from end: reads `min(size, lines * 512)` bytes
- Drops first partial line correctly
- Applies ANSI stripping only to tail
- **Estimated cost**: ~1-2ms regardless of log size

### ANSI regex patterns

Simple (`status.py:25`):
```
r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m"
```

Comprehensive (`status.py:329-336`):
```
r"\x1b\[[0-9;?]*[a-zA-Z]"      # CSI sequences
r"|\x1b\].*?(?:\x07|\x1b\\)"   # OSC sequences
r"|\x1b\[.*?m"                  # SGR color codes
r"|\x1b[()][0-9A-Za-z]"        # Character set selection
r"|\x1b[=>]"                    # Keypad modes
r"|[\x00-\x08\x0e-\x1a\x7f]"  # Control characters
```

**Backtracking risk**: The `\x1b\].*?(?:\x07|\x1b\\)` branch uses lazy `.*?` with
alternation in the terminator. If a log line contains `\x1b]` but the OSC sequence
is truncated (no `\x07` or `\x1b\\` terminator), the engine will scan to end of
string before failing. For a single log line (typically <200 chars), this is harmless.
For multi-line input passed as a single string, it could scan kilobytes. **Low risk**
in practice but could be hardened with a length cap: `\x1b\].{0,256}?(?:\x07|\x1b\\)`.

---

## 8. Parallelism: Sequential Ops That Could Be Concurrent

### 8.1 create_worker_pane: worktree setup

`lifecycle.py:354-397` — these are currently sequential but independent:

```python
# These could run in parallel:
base_sha = git rev-parse HEAD        # independent
git worktree prune                   # independent (if needed at all)
bulk_info()                          # independent (for concurrency check)
```

**Estimated savings**: ~50ms (overlap 3 forks instead of serial)

### 8.2 review_worker_pane: 4 independent git calls

`inspection.py:44-83` — all read from the same worktree, all independent:

```python
git diff --stat base..HEAD           # independent
git diff --name-only base..HEAD      # independent
git log --oneline base..HEAD         # independent
git status --porcelain               # independent
```

**Estimated savings**: ~60ms (4 × 20ms serial → 20ms parallel)

### 8.3 _compute_freshness: 3 independent git calls

`status.py:75-103`:

```python
git log base..HEAD --oneline         # independent
git diff --name-only base..HEAD      # independent (main)
git -C wt diff --name-only base..HEAD # independent (worker)
```

**Estimated savings**: ~40ms per pane

### 8.4 wait_all_worker_panes: per-pane is_alive checks

`waiter.py:329-346` — checks each pane's `_is_done` sequentially. Each calls
`is_alive` which is a separate tmux fork. Should use `bulk_info()` once per tick
and pass alive status into `_is_done`.

**Estimated savings**: `(N-1) × 15ms` per tick for N panes

### 8.5 _full_cleanup: kill + worktree removal

`lifecycle.py:479-531` — tmux kill and git worktree remove are independent:

```python
# Currently sequential:
kill_pane(pane_id)            # tmux operation
worktree_remove(worktree)    # git operation — independent of tmux
```

**Estimated savings**: ~30ms per close

### 8.6 setup_pane_borders: 2 tmux calls → 1 compound

`tmux.py:173-183` — two `_run()` calls that could be combined with `;`:

```python
_run(["set-option", ..., "pane-border-status", "top"], silent=True)
_run(["set-option", ..., "pane-border-format", ...], silent=True)
```

**Estimated savings**: ~15ms per create (but should be cached, not per-create)

---

## Quick Wins

Sorted by impact/effort ratio (highest first):

### 1. Replace `_read_last_output_from_log` with seek-from-end
- **File**: `status.py:32-45`
- **Current cost**: O(n) per pane per refresh, potentially seconds for large logs
- **Fix**: Use the same seek-from-end pattern as `tail_worker_log` with maxlines=3
- **Estimated improvement**: 100-1000x for large logs, eliminates dashboard stutter

### 2. Remove `time.sleep(0.25)` from create path
- **File**: `lifecycle.py:398`
- **Current cost**: 250ms per create, 45% of bootstrap latency
- **Fix**: Remove or replace with a `tmux display-message` readiness check (0 if shell
  already started, ~15ms if needed). The tmux `new-window` call is synchronous.
- **Estimated improvement**: 250ms per worker create

### 3. Cache `setup_pane_borders` (call once per session)
- **File**: `lifecycle.py:394` → `tmux.py:166-183`
- **Current cost**: 2 tmux forks (~30ms) per create
- **Fix**: Module-level `_borders_configured = False` flag
- **Estimated improvement**: 30ms per create (after first)

### 4. Use `bulk_info()` in wait loops instead of per-pane `is_alive`
- **Files**: `done.py:202-203`, `waiter.py:329-346`
- **Current cost**: 1 tmux fork per pane per tick (N × 15ms)
- **Fix**: Call `bulk_info()` once per tick in the wait loop, pass the alive set into
  `_is_done`. Add `alive_set` parameter to `_is_done`.
- **Estimated improvement**: `(N-1) × 15ms` per tick

### 5. Deduplicate git calls in review + freshness
- **Files**: `inspection.py:44-101`, `status.py:51-120`
- **Current cost**: 4 redundant forks per review (~80ms)
- **Fix**: Have `_compute_freshness` accept pre-computed data, or compute freshness
  inside `review_worker_pane` from data already fetched
- **Estimated improvement**: 80ms per review

### 6. Skip tmux title update for terminal states in `update_pane_state`
- **File**: `persistence.py:437-451`
- **Current cost**: 1 SELECT + 1 tmux fork after every state change, even when pane
  is dead (merged, closed, abandoned)
- **Fix**: Skip the title update when `new_state in {"merged", "closed", "superseded"}`
- **Estimated improvement**: ~35ms per terminal state transition

### 7. Parallelize independent git calls in review/freshness
- **Files**: `inspection.py:44-83`, `status.py:75-103`
- **Current cost**: 4-7 serial git forks (~80-140ms)
- **Fix**: `concurrent.futures.ThreadPoolExecutor` with `max_workers=4`
- **Estimated improvement**: ~60-100ms per review

### 8. Combine `configure_worker_pane` + `start_logging` into one compound command
- **Files**: `tmux.py:246-273`, `lifecycle.py:187-193`
- **Current cost**: 2 tmux forks (~30ms)
- **Fix**: Append `pipe-pane` to the existing compound `;` chain in `configure_worker_pane`
- **Estimated improvement**: ~15ms per create

### 9. Eliminate triple `git worktree prune`
- **Files**: `lifecycle.py:55`, `lifecycle.py:526-529`, `gitops.py:19-25`
- **Current cost**: `worktree prune` runs in `_create_worktree`, `_full_cleanup`, and
  `_remove_worktree`. A create-then-close cycle hits it 2-3 times.
- **Fix**: Remove the prune from `_create_worktree` (only prune on close/remove).
  The prune is only needed to clean stale refs from crashed workers.
- **Estimated improvement**: ~30ms per create

### 10. Batch `update_pane_state` + `emit_event` into single transaction
- **File**: `persistence.py`
- **Current cost**: 2 separate `conn.commit()` calls, each acquiring WAL write lock
- **Fix**: Combine into one `_retry_on_lock` wrapper with a single `conn.commit()`
- **Estimated improvement**: Reduces lock contention under concurrent dashboard + CLI

---

## Summary Table

| Operation | Current Forks | Avoidable | Current Latency | Saveable |
|-----------|:------------:|:---------:|:---------------:|:--------:|
| **Create** | 14 | 4 | ~560ms | ~325ms |
| **Wait** (per tick) | 1-3 | 0-N | ~15-50ms | ~(N-1)×15ms |
| **Review** | 10 | 4 | ~200ms | ~80ms |
| **Merge** | 25-35 | 3-5 | ~500-700ms | ~90ms |
| **Close** | 6-9 | 2 | ~120-180ms | ~45ms |
| **Dashboard** (per refresh) | 1 + N | N | O(n) per pane | **orders of magnitude** |

The dashboard `_read_last_output_from_log` bug dwarfs all other findings.
Fix it first; the rest are polish.
