# State and events

dgov maintains a robust record of all operations in a SQLite database. This data is used for state persistence, progress tracking, and auditing (blame). Both state and events are stored in `.dgov/state.db`.

## State database

The primary state store is a **SQLite-only** database located at `.dgov/state.db`. The database uses **WAL (Write-Ahead Logging)** mode to allow concurrent access from multiple processes.

### Table: `panes`

Each record represents a worker pane and its current lifecycle state.

| Column | Type | Description |
|--------|------|-------------|
| `slug` | TEXT | Primary key. Unique task identifier. |
| `prompt` | TEXT | The task prompt sent to the agent. |
| `pane_id` | TEXT | The backend's unique ID (e.g., tmux pane ID). |
| `agent` | TEXT | The agent ID (e.g., `claude`). |
| `project_root`| TEXT | Path to the repository root. |
| `worktree_path`| TEXT | Path to the worker's isolated worktree. |
| `branch_name` | TEXT | Name of the worker's git branch. |
| `created_at` | REAL | Unix timestamp of creation. |
| `owns_worktree`| INTEGER | Whether dgov owns this worktree (bool stored as 0/1). |
| `base_sha` | TEXT | The git commit hash the worker started from. |
| `parent_slug` | TEXT | Slug of the parent pane (for LT-GOV workers). |
| `tier_id` | TEXT | DAG tier identifier. |
| `role` | TEXT | Pane role: `worker`, `governor`, or `lt-gov`. |
| `state` | TEXT | Canonical state (see below). |
| `metadata` | TEXT | JSON blob for extra fields (e.g., `max_retries`). |

### CRUD functions

State is managed through focused helper functions in `persistence.py` — there are no generic `_read_state`/`_write_state` functions:

| Function | Purpose |
|----------|---------|
| `get_pane(session_root, slug)` | Retrieve a single pane record by slug. |
| `all_panes(session_root)` | Return all pane records as a list of dicts. |
| `list_panes_slim(session_root)` | List all panes with truncated prompt text (fast). |
| `add_pane(session_root, pane)` | Insert a `WorkerPane` dataclass into the database. |
| `remove_pane(session_root, slug)` | Delete a pane record by slug. |
| `update_pane_state(session_root, slug, new_state, force=False)` | Update state with transition validation. |
| `set_pane_metadata(session_root, slug, **kwargs)` | Update metadata fields (atomic). |
| `replace_all_panes(session_root, panes)` | Replace all records (test setup helper). |

## Pane states

A pane can only be in one of these 12 canonical states:

- `active`: The agent is running.
- `done`: Task complete, changes committed.
- `failed`: Agent exited with error or failed to commit.
- `reviewed_pass`: Manual or auto-review passed.
- `reviewed_fail`: Review found issues.
- `merged`: Branch merged into `main`.
- `merge_conflict`: Merge failed due to conflicts.
- `timed_out`: Max execution time exceeded.
- `escalated`: Task handed off to a stronger agent.
- `superseded`: Original pane replaced by a retry attempt.
- `closed`: Resources (worktree, pane) cleaned up.
- `abandoned`: Task discarded without merging.

## State machine enforcement

Every state change for a worker pane is validated against the `VALID_TRANSITIONS` table in `persistence.py`. This ensures that a pane cannot move, for example, from `merged` back to `active`, or from `closed` to `done`.

Illegal transitions raise `IllegalTransitionError(ValueError)`, which includes the current state, target state, and slug for debugging. Same-state transitions are no-ops (no error). The `force=True` flag bypasses validation when needed (e.g., cleanup operations).

## Event journal

The event journal is a dedicated `events` table in `.dgov/state.db`. Every significant state change writes a structured event via `emit_event()`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Primary key. |
| `ts` | TEXT | ISO 8601 UTC timestamp. |
| `event` | TEXT | Event type (e.g., `pane_created`). |
| `pane` | TEXT | Slug of the associated pane. |
| `data` | TEXT | JSON blob for event-specific metadata. |

**Event types (`VALID_EVENTS`):**
- **Pane:** `pane_created`, `pane_done`, `pane_resumed`, `pane_timed_out`, `pane_merged`, `pane_merge_failed`, `pane_escalated`, `pane_superseded`, `pane_closed`, `pane_retry_spawned`, `pane_auto_retried`, `pane_blocked`, `pane_auto_responded`, `pane_circuit_breaker`.
- **DAG:** `dag_started`, `dag_tier_started`, `dag_task_dispatched`, `dag_task_completed`, `dag_task_failed`, `dag_task_escalated`, `dag_tier_completed`, `dag_completed`, `dag_failed`.
- **Mission:** `mission_pending`, `mission_running`, `mission_waiting`, `mission_reviewing`, `mission_merging`, `mission_completed`, `mission_failed`.
- **Other:** `checkpoint_created`, `review_pass`, `review_fail`, `experiment_started`, `experiment_accepted`, `experiment_rejected`, `review_fix_started`, `review_fix_finding`, `review_fix_completed`, `merge_enqueued`, `merge_completed`, `yap_received`.

## Blame

The `dgov blame` command uses the event table and git history to attribute changes back to specific agents and tasks.

```bash
dgov blame src/parser.py
```

dgov resolves attribution by:
1. Finding the last git commit that touched the file.
2. Checking if the commit subject matches a merge pattern (`Merge branch '...'`).
3. Mapping the branch name or commit hash back to a slug in the event table.

## Checkpoints

A checkpoint is a snapshot of the `state.db` file. Checkpoints are stored in `.dgov/checkpoints/<name>/`.

```bash
# Create a snapshot
dgov checkpoint create "before-massive-refactor"

# List available snapshots
dgov checkpoint list
```
