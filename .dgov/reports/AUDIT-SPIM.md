# SPIM Adversarial Audit Report

Date: 2026-03-17
Auditor: Hunter Alpha (adversarial review)

---

## P0 (will crash or corrupt data)

### Connection leak in `ensure_schema` on concurrent creation race
**models.py:94-111** — Two threads calling `ensure_schema` for the same path simultaneously will both pass the initial cache check. Thread A caches first. Thread B detects the race, closes its connection, returns A's. But if `conn.commit()` or any schema DDL raises between creation and the second `_CONN_LOCK` block, Thread B's connection leaks (never closed, never cached).

```
Thread A: check cache (miss) → create conn → schema DDL → commit → cache conn ✓
Thread B: check cache (miss) → create conn → schema DDL → [exception here] → LEAK
```

SQLite connection objects hold file descriptors. Under load, this leaks FDs.

---

## P1 (incorrect behavior)

### `RegionLockManager.acquire` upsert relies on unspecified `rowcount` behavior
**locking.py:27-42** — The `ON CONFLICT(region) DO UPDATE ... WHERE` pattern returns `rowcount = 0` when the WHERE clause fails (lock held by different non-expired agent). This works on current SQLite but the SQL standard does not guarantee `rowcount` semantics for skipped conflict updates. A future SQLite version or an alternative driver could break this, silently allowing lock double-acquisition.

### State machine violation: `Governor.retarget` can transition "done" → "watching"
**governor.py:83-87** — `retarget` calls `update_agent(..., status="watching")` without checking the agent's current status. An agent in terminal state "done" is resurrected to "watching". No state transition guard exists anywhere in the codebase.

### State machine violation: `Governor.escalate` can transition "done" → "idle"
**governor.py:93-97** — Same issue. An expired/completed agent can be re-escalated.

### State machine violation: `SimEngine.observe` can transition any status → "watching"
**engine.py:54** — `observe()` calls `update_agent(..., status="watching")` unconditionally. An agent in "acting", "done", or "blocked" is moved to "watching", bypassing the intended state flow.

### Orphaned agent status when `_process_claim` raises after `mark_delta_applied`
**engine.py:166-213** — If `mark_delta_applied` succeeds but `update_claim_status` or `update_agent` raises (lines 199-200), the `finally` block releases the lock but the agent remains in "acting" status permanently. The claim is stuck in "accepted" state (delta already applied). No recovery path exists — the agent is zombied until manual intervention or TTL expiry.

### Blocked claims are reprocessed every tick
**engine.py:147-149** — `tick()` filters for `{"accepted", "blocked"}` status. Blocked claims (failed lock acquisition) are retried every tick. If the region remains contested, this spins doing useless work. The claim has no backoff, jitter, or retry limit. Under contention this creates a busy loop.

### `Governor.reject` sets agent "done" without checking other active claims
**governor.py:69** — `reject` sets the agent to "done" unconditionally. If the agent has other pending/accepted claims for different regions, those claims become orphaned — the agent won't process them because it's in terminal state.

### `check()` has TOCTOU race between read and delete
**locking.py:57-64** — `check()` reads the lock, compares expiry, then deletes if expired. Between the read and the delete, another agent could acquire the same region. The delete then removes the new agent's lock. This is unlikely but possible under high contention.

### String comparison of ISO timestamps in `check()`
**locking.py:62** — `str(lock["expires_at"]) <= models.isoformat_utc(self._now_fn())` compares ISO 8601 strings lexicographically. This works for UTC timestamps with identical precision but breaks if timestamps have different timezone offsets (e.g., `+00:00` vs `Z`) or different fractional-second precision.

---

## P2 (missing coverage)

### Zero tests for error/edge paths
No tests exercise:
- `update_agent` with non-existent `agent_id` (should raise `ValueError`)
- `update_claim_status` with non-existent `claim_id`
- `create_claim` with invalid agent_id (FK violation)
- `mark_delta_applied` / `mark_delta_reverted` with non-existent `delta_id`
- `expire_agents` with mixed statuses
- `_retry_on_lock` hitting max retries

### Zero tests for Governor methods: `reject`, `retarget`, `escalate`
These methods have no test coverage at all.

### Zero tests for `close_cached_connections()`
The cache teardown path is untested.

### Zero tests for `EventBus` wildcard subscriber
`engine.py:38-39` — The `*` wildcard subscription pattern is untested.

### Zero tests for `SimEngine.observe()` and `SimEngine.act()`
Only `propose` → accept → tick flow is tested.

### Zero tests for `run()` method
**engine.py:143-152** — Multi-tick execution with sleep is untested.

### No test for expired agent cleanup end-to-end
The test `test_region_lock_manager_expires_stale_locks` tests lock expiry but no test verifies the full `expire_agents` → `agent_expired` event → lock cleanup cycle.

### No test for concurrent lock acquisition from multiple agents
All lock tests use sequential agents. No test verifies that 3+ agents competing for the same region produces correct mutual exclusion.

### No test for `_apply_patch` with non-dict existing state
**engine.py:215-221** — When `self.state[region]` is not a dict (e.g., a list or string), the patch silently replaces it. No test covers this branch.

### `Foreign keys ON` but no `ON DELETE CASCADE`
Deleting an agent leaves orphaned claims, events, locks, and deltas. This is a design choice for auditability, but there's no test verifying the system handles orphaned rows gracefully.

---

## Summary

| Severity | Count |
|----------|-------|
| P0       | 1     |
| P1       | 8     |
| P2       | 10    |

**Most critical finding:** The connection leak in `ensure_schema` under concurrent access will exhaust file descriptors under load.

**Most common pattern:** Missing state transition guards. The codebase validates input values (`_validate_agent_status`) but never checks *current* status before transitions, allowing arbitrary state machine jumps.
