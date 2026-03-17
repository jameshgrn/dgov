## Audit: Done Detection & Waiting

### Critical Bugs

**1. `_poll_once` discards grace-period state across polls — abandonment never triggers**
`waiter.py:81-120` / `done.py:218-241`

`_poll_once` builds a fresh `stable_state` dict each call and only extracts `last_output`, `stable_since`, `last_blocked` on return. But `_is_done` uses `dead_since` and `commit_seen_at` keys to track cross-poll grace periods. These keys are set inside `_stable_state` but never propagated back to the caller.

Result: `wait_worker_pane` (line 215) and `wait_all_worker_panes` (line 290) lose grace-period tracking every poll. For "abandoned" detection, `dead_since` is set on first poll, then lost on the next poll, so the 10-second grace timer never advances. The pane is never marked abandoned via `_poll_once`-based callers.

`wait_for_slugs` (line 172) is **not** affected — it passes the full `stable_state` dict to `_is_done` directly, preserving grace state.

Severity: P0 — workers are never marked abandoned when using `wait_worker_pane` or `wait_all_worker_panes`.

Fix: Either have `_poll_once` return the full `stable_state` dict (not just three fields), or restructure `_is_done` to return grace-period timestamps alongside the done boolean.

---

**2. Exit-signal path emits no event**
`done.py:145-152`

When the `.exit` file is detected, `_is_done` updates state to "failed" but never calls `emit_event(session_root, "pane_done", slug)`. The commit path (line 208) does emit the event. This means failed panes (crashes, nonzero exits) are silently recorded without triggering downstream event listeners (dashboards, notifications, governor).

Severity: P0 — silent state transitions with no event.

Fix: Add `_persist.emit_event(session_root, "pane_done", slug)` before the return on line 152.

---

**3. Done-signal path emits no event**
`done.py:136-143`

Same issue as above: the done-signal file path updates state but doesn't emit `pane_done`. The only reason this doesn't cause total event loss is that `list_worker_panes` and other callers eventually pick up the state change. But real-time event consumers miss it.

Severity: P1 — events are missed but eventual consistency exists.

Fix: Add `_persist.emit_event(session_root, "pane_done", slug)` before the return on line 143.

---

### Logic Errors

**4. `_resolve_strategy` defaults to "api" when no strategy given**
`done.py:80-90`

The docstring in `agents.py` says "signal" is the default strategy. But `_resolve_strategy` returns `("api", 0)` when `done_strategy is None`. This means any caller that doesn't explicitly pass a strategy (including `wait_for_slugs` which receives `None` from `_strategy_for_pane` when the pane has no agent) gets "api" behavior — signal files and liveness only, with commit/stable skipped.

Severity: P1 — silent behavioral change from documented defaults.

Fix: Change line 90 to `return "signal", stable_seconds or 15` to match the documented default.

---

**5. `_has_new_commits` has no guards on `project_root` or `branch_name`**
`done.py:36-46`

The function guards against empty `base_sha` but blindly passes `project_root` and `branch_name` to git. If either is empty/None, git receives an invalid `-C` argument or logs a confusing error. Callers like `poll_workers` (monitor.py:71) pass potentially empty strings from pane records.

Severity: P2 — git errors are swallowed (return False), but the error messages pollute stderr.

Fix: Add `if not project_root or not branch_name: return False` at the top.

---

**6. Monitor's `_take_action` skips state persistence for auto-complete timing**
`monitor.py:113-114`

`_take_action` reads the pane state from the DB to avoid TOCTOU (line 108), but then calls `_auto_complete` which writes to the done-signal file and updates state. The monitor then continues evaluating other workers in the same tick. If another worker's classification depends on the first worker's state, it sees stale data for the rest of the tick.

Severity: P2 — cross-worker state visibility is one tick behind.

Fix: After `_auto_complete`, refresh the pane record from DB, or evaluate workers in a loop that re-checks state.

---

### Inefficiencies

**7. `list_worker_panes` redundantly fetches `current_command` per pane**
`status.py:163-170`

`list_worker_panes` already captures `cmd = all_tmux.get(pane_id, {}).get("current_command", "")` from the bulk tmux call. But when calling `_is_done` on line 170, it doesn't pass `current_command=cmd`. Inside `_is_done`, the agent process check calls `get_backend().current_command(pane_id)` again — a redundant per-pane backend call.

Severity: P2 — extra backend calls in a hot path.

Fix: Pass `current_command=cmd` to `_is_done` on line 170.

---

**8. `prune_stale_panes` uses per-pane `is_alive` instead of bulk**
`status.py:370-375`

Each pane triggers a separate `get_backend().is_alive(pane_id)` call. `list_worker_panes` uses `get_backend().bulk_info()` for a single bulk call. `prune_stale_panes` should do the same.

Severity: P2 — O(N) subprocess calls instead of 1.

Fix: Fetch `all_tmux = get_backend().bulk_info()` once, then check `pane_id in all_tmux`.

---

**9. `_compute_freshness` spawns up to 3 git subprocesses per pane**
`status.py:68-116`

For each pane with freshness enabled, `_compute_freshness` runs `git log`, `git diff` (main), and `git diff` (worker). With 10 panes, that's 30 subprocess forks. The `worker_changed_files` parameter exists to skip the worker diff, but `list_worker_panes` never uses it.

Severity: P3 — significant overhead in dashboard/pre-fligh paths.

Fix: Consider batching git operations or caching results across panes with the same `base_sha`.

---

**10. `_poll_once` rebuilds stable_state dict every call**
`waiter.py:86-92`

Every poll cycle constructs a new dict, copies in the previous values, passes it to `_is_done`, then extracts 3 values back out. Keys added by `_is_done` (like `commits_detected`, `commit_count`, `_cb_prev_hash`) are silently discarded. This forces redundant work on the next poll (e.g., recomputing commit count, resetting circuit breaker tracking).

Severity: P3 — wasted computation and lost optimization state.

Fix: Maintain the stable_state dict across polls (as `wait_for_slugs` already does).

---

### Dead Code

**11. `_CIRCUIT_BREAKER_LINES` used only once**
`done.py:24`

`_CIRCUIT_BREAKER_LINES = 20` is a module-level constant used only on line 301. Not strictly dead, but if it's meant to be configurable, it should be exposed; if not, inline it.

Severity: P3 — minor style issue.

---

**12. Duplicate agent command sets**
`done.py:52-65` vs `status.py:235-250`

`_AGENT_COMMANDS` in done.py and the inline `agent_cmds` set in `list_worker_panes` contain overlapping but not identical sets. `_AGENT_COMMANDS` is a frozenset; `agent_cmds` is a regular set rebuilt on every pane. Any update to one must be manually mirrored to the other.

Severity: P2 — maintenance hazard, potential divergence.

Fix: Extract a single `AGENT_COMMANDS` constant and import it in both modules.

---

### Minor Issues

**13. `_set_done_reason` called with `stable_state=None` is silently ignored**
`done.py:96-98`

When `stable_state` is None, `_set_done_reason` is a no-op. This is by design, but callers in the done-signal (line 142) and exit-signal (line 151) paths invoke it with None, then return True. The caller never learns *why* the pane was done. This is cosmetic but hurts debuggability.

Severity: P3.

Fix: Consider logging the reason when stable_state is None.

---

**14. `_nudged_slugs` is module-lifetime, never cleaned up**
`waiter.py:34`

Once a slug is nudged, it's never removed from the set. For long-running daemon processes, this is a slow memory leak (each slug is ~20 bytes). Not a real issue for typical workloads, but worth noting.

Severity: P3.

Fix: Use a bounded structure (e.g., `collections.deque(maxlen=1000)`) or remove slugs when they complete.

---

**15. `nudge_pane` has a blocking `time.sleep(wait_seconds)`**
`waiter.py:424`

The nudge function blocks the calling thread for `wait_seconds` (default 10s). If called from an async context or a UI thread, this freezes the caller. No timeout on `send_input` either.

Severity: P3.

Fix: Document the blocking behavior, or make it async/callback-based.

---

**16. `wait_worker_pane` doesn't reset timeout on auto-retry**
`waiter.py:240-250`

When a pane fails and is auto-retried, the function switches to the new slug but keeps the original `start` timestamp. If the original pane took 550s of a 600s timeout, the retry gets only 50s. This may be intentional (total budget) but is undocumented.

Severity: P3 — potential for premature timeout on retries.

Fix: Document the behavior, or reset `start` on retry if per-retry timeout is desired.
