## Audit: UI & Ancillary Modules

### Critical Bugs
- None observed in the reviewed UI and ancillary modules.

### Logic Errors
- src/dgov/recovery.py:186-228, `retry_or_escalate` may return a generic escalation error when `next_agent == current_agent`, but this situation can also arise if `_resolve_escalation_target` fails to find a configured escalation path rather than a true exhaustion-of-retries condition; this blurs causes and makes upstream handling harder to reason about. Severity P3. Suggested fix: distinguish between "no escalation target configured" and "retries exhausted" in the error payload (e.g., add a `reason` field or separate error codes) so callers can decide whether to adjust config vs. change retry policy.

### Inefficiencies
- src/dgov/terrain.py:111-145, `ErosionModel.step` allocates fresh `cells`, `receiver`, `slope`, and `area` lists on every step, which is potentially expensive for larger grids and high refresh rates. Severity P3. Suggested fix: preallocate these arrays once on model construction (or cache and reuse them between steps) and only clear/overwrite their contents per step, which will significantly reduce allocations and GC pressure in long-running dashboards.
- src/dgov/inspection.py:44-55, `review_worker_pane` spawns up to five separate `git` subprocesses per call (stat, names, log, status, full diff) even when `full=False`, which can be noticeable latency in tight review loops. Severity P3. Suggested fix: gate the expensive `--stat` and `log` calls behind flags when only a minimal verdict is needed, or reuse results from a cheaper `git diff --name-status` where possible.
- src/dgov/preflight.py:163-202, `check_deps` runs `uv sync --locked` even when invoked frequently, and the default timeout is only 30s which may cause repeated heavy syncs under contention. Severity P3. Suggested fix: keep the current behavior for explicit `dgov preflight` but cache a recent successful deps check timestamp and skip re-running within a short TTL unless the caller explicitly asks for a full deps validation.

### Dead Code
- No clear dead code (unreachable branches or unused top-level functions) identified in the reviewed files; imports and helpers appear to be referenced.

### Minor Issues
- src/dgov/dashboard_v2.py:381-394, `_acquire_dashboard_lock` kills an existing PID and then unconditionally overwrites the pidfile without confirming that the old process actually terminated, which can lead to brief races where two dashboards contend on the same session. Severity P3. Suggested fix: after sending SIGTERM, poll with `os.kill(old_pid, 0)` for a short bounded window before accepting the lock, and if the process remains alive, abort with a clear error instead of taking over.
- src/dgov/inspection.py:148-156, `diff_worker_pane` runs `git diff` without a timeout, so a blocked or interactive `git` (e.g., due to credential helpers or hooks) can hang the caller. Severity P3. Suggested fix: add a conservative timeout (e.g., 20–30 seconds) to the `subprocess.run` call and surface a structured timeout error message to callers.
- src/dgov/openrouter.py:165-179 and 193-208, `chat_completion` and `chat_completion_local_first` collapse all underlying provider errors into a generic `RuntimeError("All LLM providers failed ...")`, which hides whether failure was due to auth, connectivity, or model issues. Severity P3. Suggested fix: include the last-seen provider error (or a summarized list of causes) in the raised exception payload so higher layers and users can distinguish configuration problems from transient outages.

## Audit: UI & Ancillary Modules

### Critical Bugs

1. **dashboard_v2.py:156** — Unbounded `read_events` call loads entire event log into memory every refresh cycle. As the events table grows, this becomes an OOM risk and a hot-path performance killer. **Severity: P1**
   - Fix: Pass `limit=8` (matching the intended UI display): `read_events(session_root, limit=8)`.

2. **terrain_pane.py:119** — `read_events(pr)` called with no limit every 5 seconds. The full event history is loaded, translated, and applied to the terrain model on every poll. With a long session this becomes increasingly expensive. **Severity: P1**
   - Fix: Pass `limit=100` or implement a cursor-based approach to only process new events since last poll.

3. **dashboard_v2.py:190-196** — PID file race condition in `_acquire_dashboard_lock`. The function checks `os.kill(old_pid, 0)` to test liveness, then kills the process with SIGTERM. If two dashboards start simultaneously, both could pass the liveness check before either writes its PID, resulting in both running or one killing the other unexpectedly. **Severity: P1**
   - Fix: Use `fcntl.flock()` or atomic `os.open(O_CREAT|O_EXCL)` for proper exclusive locking.

4. **inspection.py:107-110** — `read_events(session_root)` called with no limit loads the entire events table to count retries and auto-responses. This runs on every `review_worker_pane` call, which is a frequent operation. **Severity: P1**
   - Fix: Pass `limit=500` or query counts directly via SQL: `SELECT COUNT(*) FROM events WHERE event = 'pane_retry_spawned' AND pane = ?`.

### Logic Errors

5. **dashboard_v2.py:312-320, 343-351** — Potential index-out-of-bounds crash. The `selected` index is read under lock, but the `panes` list is copied and then accessed by index. If `fetch_panes` runs between the lock release and the index access (shrinking the list), `panes[sel]` will raise `IndexError`. **Severity: P2**
   - Fix: Bounds-check `sel` against `len(panes)` after acquiring the copy, before indexing.

6. **dashboard_v2.py:296-310** — Merge/close confirmation dialog is fragile. `live.stop()` is called to print the prompt, but if the user input or `_execute_action` raises, `live.start()` is never called and the terminal is left in raw mode with no Live display. The outer `try/finally` doesn't re-enter the Live context. **Severity: P2**
   - Fix: Wrap the stop/start/action block in its own try/except to guarantee `live.start()` and `tty.setcbreak()` are restored.

7. **openrouter.py:227-233** — `check_status` fetches key info via `get_key_info()` (which calls the `/auth/key` endpoint), then separately pings `/models` to test reachability. The models ping is redundant since `get_key_info` already proves API reachability. More importantly, if the key has no models-list permission, the `/models` call fails and marks `api_reachable=False` even though the key works. **Severity: P2**
   - Fix: Set `api_reachable=True` if `get_key_info` succeeds; only fall back to `/models` ping if `get_key_info` fails.

8. **recovery.py:146** — `re.sub(r"-\d+$", "", slug)` strips trailing numeric suffix for retry slug computation. If a legitimate slug ends in `-3` (not a retry), this incorrectly computes `base_slug` and the retry counter resets. Example: slug `fix-bug-3` becomes `fix-bug`, retry becomes `fix-bug-2` (collision with a possible existing pane). **Severity: P2**
   - Fix: Use a more specific pattern like `re.sub(r"-(\d+)$", "", slug)` only when the slug matches the retry pattern, or use a metadata field instead of slug parsing.

9. **terrain.py:194** — `EventTranslator.translate` uses string timestamp comparison (`ts <= self._last_ts`) to deduplicate events. If two events share the same timestamp (common in fast operations), the second is silently dropped. **Severity: P2**
   - Fix: Use event ID or add a sequence number; or use `<` instead of `<=` and track event IDs.

### Inefficiencies

10. **openrouter.py:47-63** — `_load_config()` re-reads and re-parses `~/.dgov/config.toml` from disk on every call to `_get_api_key()`, `_get_default_model()`, and indirectly every API request. No caching. **Severity: P2**
    - Fix: Add a module-level cache with mtime-based invalidation, similar to `_free_models_cache`.

11. **terrain.py:374-410** — `overlay_stamps` recomputes character offsets by iterating all lines and summing lengths for every stamp. For N stamps and L lines, this is O(N*L). Could be O(N) with a precomputed offset table. **Severity: P3**
    - Fix: Precompute a `line_offsets` list once: `offsets = [0]` then `offsets.append(offsets[-1] + len(line) + 1)`.

12. **dashboard_v2.py:67-72** — `_branch_cache` is a module-level dict with TTL-based eviction but no size limit. In a long-running dashboard with many project roots, this grows unbounded. **Severity: P3**
    - Fix: Use `functools.lru_cache(maxsize=16)` or a bounded dict.

13. **dashboard_v2.py:85-100** — Log tail fallback reads the worker log file for every pane that has no summary and no activity. With N panes, this is N file reads per refresh cycle. **Severity: P3**
    - Fix: Cache log tails or only read for the selected pane.

14. **preflight.py:279-289** — `check_stale_worktrees` calls `list_worker_panes(project_root, ...)` without passing `session_root`. If the session root differs from project root, stale worktrees won't be detected correctly. **Severity: P2**
    - Fix: Accept and forward `session_root` parameter.

### Dead Code

15. **dashboard_v2.py:42** — `_STARTUP_TIME` is used only for stale-binary detection (line 141), which itself is a minor optimization. The entire stale-binary check could be removed if not needed in production. **Severity: P3**

16. **dashboard_v2.py:34** — `_INPUT_POLL_INTERVAL = 0.05` is used for `select` timeout, but `_UI_REFRESH_PER_SECOND = 2` implies a 500ms refresh. The input polling is 10x faster than the UI refresh, wasting CPU on tight select loops when no input arrives. **Severity: P3**
    - Fix: Set `_INPUT_POLL_INTERVAL` to `1.0 / _UI_REFRESH_PER_SECOND` (0.5).

17. **terrain_pane.py:31** — `_STARTUP_DELAY_S = 0.3` introduces an arbitrary 300ms sleep before starting the Live display. No reason is documented; appears to be a workaround for a race condition that should be fixed differently. **Severity: P3**

18. **openrouter.py:34** — `_REFERER = "https://github.com/jameshgrn/dgov"` — hardcoded referer URL. If the repo moves or forks, this is stale. Not a bug but a maintenance hazard. **Severity: P3**

### Minor Issues

19. **dashboard_v2.py:114** — `preview_lines = [ln for ln in raw.splitlines() if ln.strip()][-5:]` filters blank lines then takes last 5. This means fewer than 5 lines are shown if there are blanks. The intent seems to be "last 5 non-blank lines" but the UI title says "Output: {slug}" suggesting a fixed-size preview. **Severity: P3**
    - Fix: Take last 5 raw lines first, then filter: `raw.splitlines()[-5:]`.

20. **recovery.py:12** — Top-level imports from `dgov.lifecycle` and `dgov.persistence` create module-level coupling. If these modules have side effects at import time (e.g., DB init), importing recovery triggers them. Not currently a problem but fragile. **Severity: P3**

21. **inspection.py:110** — `auto_respond_count` is computed by iterating all events. This could be a SQL `COUNT(*)` query. **Severity: P3**
    - Fix: `SELECT COUNT(*) FROM events WHERE event = 'pane_auto_responded' AND pane = ?`.

22. **preflight.py:210** — `check_agent_health` uses `shell=True` for subprocess calls. This is a security risk if agent health_check commands come from untrusted config. **Severity: P2**
    - Fix: Use `shell=False` and pass a list of args, or validate the command string.

23. **terrain.py:35** — `_spawn_position_from_slug` uses MD5 for deterministic hashing. MD5 is fine for non-cryptographic use but a comment would prevent future "security fix" PRs from breaking the feature. **Severity: P3**
    - Fix: Add comment: `# Non-cryptographic; used only for deterministic positioning`.

24. **dashboard_v2.py:197** — `pidfile.write_text(str(os.getpid()))` is not atomic. If the process crashes between write and actual use, a partial PID could be written. **Severity: P3**
    - Fix: Write to a temp file then `os.rename()` (atomic on POSIX).

25. **openrouter.py:317-322** — `list_free_models` has a potential race on global `_free_models_cache` and `_free_models_cache_time` in multi-threaded contexts (e.g., dashboard data thread + API calls). **Severity: P3**
    - Fix: Use `threading.Lock` or accept the benign race (worst case: one extra API call).

---

## Summary

| Severity | Count | Key Concerns |
|----------|-------|--------------|
| P1       | 4     | Unbounded DB reads (3 locations), PID file race |
| P2       | 8     | Index crash, Live display leak, slug collision, config reread, shell=True, session_root passthrough |
| P3       | 13    | Performance micro-optimizations, dead constants, style |

**Recommended immediate fixes (P1):**
1. Add `limit=` to all `read_events()` calls in `inspection.py`, `terrain_pane.py`, and `dashboard_v2.py`
2. Add bounds checking before indexing `panes[selected]` in dashboard key handlers
3. Wrap dashboard merge/close dialogs in try/finally for Live display safety
4. Replace PID file locking with `fcntl.flock()`
