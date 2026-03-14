# State and events

dgov maintains a robust record of all operations in a SQLite database and an append-only event journal. This data is used for state persistence, progress tracking, and auditing (blame).

## State database

The primary state store is a SQLite database located at `.dgov/state.db`. It uses **WAL (Write-Ahead Logging)** mode to allow concurrent access from multiple processes.

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
| `base_sha` | TEXT | The git commit hash the worker started from. |
| `state` | TEXT | Canonical state (see below). |
| `metadata` | TEXT | JSON blob for extra fields (e.g., `max_retries`). |

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

## Event journal

The event journal is an append-only JSONL file at `.dgov/events.jsonl`. Every significant state change writes a structured event.

```json
{
  "ts": "2026-03-12T18:22:10.123456+00:00",
  "event": "pane_created",
  "pane": "fix-parser-1",
  "agent": "claude"
}
```

**Common event types:**
- `pane_created`, `pane_done`, `pane_merged`, `pane_timed_out`.
- `checkpoint_created`.
- `review_fix_finding`, `review_fix_completed`.
- `experiment_started`, `experiment_accepted`.

## Blame

The `dgov blame` command uses the event journal and git history to attribute changes back to specific agents and tasks.

```bash
dgov blame src/parser.py
```

dgov resolves attribution by:
1. Finding the last git commit that touched the file.
2. Checking if the commit subject matches a merge pattern (`Merge branch '...'`).
3. Mapping the branch name or commit hash back to a slug in the event journal.

## Checkpoints

A checkpoint is a snapshot of both the `state.db` and `events.jsonl`. Checkpoints are stored in `.dgov/checkpoints/<name>/`.

```bash
# Create a snapshot
dgov checkpoint create "before-massive-refactor"

# List available snapshots
dgov checkpoint list
```
