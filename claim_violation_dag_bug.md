# Fix: Map claim_violation events to DAG kernel DagEvents

## Problem

When the merger detects a file claim violation (e.g., a pane modified files outside its declared file_claims), it emits a `claim_violation` event. However:

1. There is NO entry in `_DAG_EVENT_FACTORY` in `src/dgov/monitor.py` to translate this to a kernel DagEvent
2. There is NO `TaskClaimViolation` DagEvent type in `src/dgov/kernel.py`
3. The kernel never processes the claim violation
4. The DAG task remains in an intermediate state (likely REVIEWING or MERGE_READY)
5. The DAG eventually fails or hangs because the task never reaches a terminal state

This causes manual intervention to be required—exactly what happened with `fix-readonly-stall` (run 113), where the work was done correctly but the DAG reported failure.

## Evidence

- Event: `r113-fix-readonly-stall` emitted `claim_violation` at 2026-03-28T15:36:09
- The pane actually completed successfully (commits on branch `r113-fix-readonly-stall`)
- But the DAG task status was never updated to reflect completion
- DAG run 113 status: `failed`

## Solution

Add claim_violation handling to the DAG event pipeline:

1. **Add `TaskClaimViolation` DagEvent** in `src/dgov/kernel.py`:
   ```python
   @dataclass
   class TaskClaimViolation:
       ts: float
       task_slug: str
       reason: str
   ```

2. **Add to `_DAG_EVENT_FACTORY`** in `src/dgov/monitor.py`:
   ```python
   "claim_violation": lambda ts, sl, ev: TaskClaimViolation(ts, sl, ev.get("reason", "file_claim_violation")),
   ```

3. **Handle in kernel `handle` method** in `src/dgov/kernel.py`:
   - Mark task as FAILED
   - Continue scheduling other tasks
   - DAG will complete as PARTIAL or FAILED

## Files to change

- `src/dgov/kernel.py` — add TaskClaimViolation event type and handler
- `src/dgov/monitor.py` — add claim_violation to _DAG_EVENT_FACTORY

## Tests needed

- Unit test: claim_violation event is correctly mapped to TaskClaimViolation
- Unit test: kernel handles TaskClaimViolation by marking task FAILED
- Integration test: DAG with claim_violation completes with PARTIAL status
