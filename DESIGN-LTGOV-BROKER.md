# Brokered LT-GOV Design

## Status

Proposed replacement for the prompt-only LT-GOV model in [`/Users/jakegearon/projects/dgov/DESIGN-V2.md`](/Users/jakegearon/projects/dgov/DESIGN-V2.md).

This design keeps the governor as the only trusted executor of mutating `dgov` actions. LT-GOV panes become planners and request writers, not sub-governors with ambient CLI authority.

## Design Goals

1. Preserve one authority boundary: only the governor process creates, merges, retries, escalates, reviews, and closes panes.
2. Make LT-GOV topology durable in SQLite instead of inferring it from prompts or filenames.
3. Replace shared mutable JSON files with append-only SQL records.
4. Detect LT-GOV death with heartbeats and quarantine children before they drift.
5. Make tier restart semantics explicit with epochs.

## Non-Goals

1. This is not OS-level sandboxing. A same-user process can still tamper with files if it chooses to bypass the protocol.
2. This does not make SQLite a distributed scheduler. It makes writes simple, append-only where possible, and governor-serialized for mutating actions.

## Core Invariants

1. LT-GOV panes never execute `dgov pane create|merge|close|retry|escalate|review|util`.
2. Every LT-GOV request is a row in `command_requests`.
3. Every pane in a tier carries `tier_id` and `epoch`.
4. Every LT-GOV-created child carries `assigned_by=<ltgov-slug>`.
5. The governor rejects any request whose `epoch` is not the current epoch for that requester and tier.
6. Parent death never causes implicit child adoption.

## Actor Model

- Governor process
  - Polls `command_requests` at 1 Hz.
  - Validates request scope.
  - Executes approved requests by calling existing internal Python functions, not by shelling out to the CLI.
  - Runs the heartbeat reconciliation loop.
- LT-GOV pane
  - Reads repo state and dashboard state.
  - Writes append-only `progress_events`, `tier_summaries`, `advisories`, `heartbeat_events`.
  - Inserts typed rows into `command_requests`.
  - Never mutates pane state directly.
- Plain worker pane
  - Same behavior as today.
  - No queue usage unless later promoted into a tiered workflow.

## Schema Changes

### `panes` additions

`panes` is the durable topology table. Metadata JSON is no longer authoritative for hierarchy or scope.

```sql
ALTER TABLE panes ADD COLUMN parent_slug TEXT;
ALTER TABLE panes ADD COLUMN tier_id TEXT;
ALTER TABLE panes ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE panes ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'governor';

CREATE INDEX IF NOT EXISTS idx_panes_parent_slug
  ON panes(parent_slug);

CREATE INDEX IF NOT EXISTS idx_panes_tier_epoch
  ON panes(tier_id, epoch);

CREATE INDEX IF NOT EXISTS idx_panes_assigned_by_epoch
  ON panes(assigned_by, epoch);
```

Field semantics:

- `parent_slug`
  - Immediate supervisor pane.
  - `NULL` for direct governor-created panes, including LT-GOV roots.
- `tier_id`
  - `NULL` for plain governor-managed workers.
  - String like `tier-1` for an LT-GOV root and all children in that tier.
- `epoch`
  - `0` for plain workers.
  - Positive integer for LT-GOV tiers.
  - Incremented when a tier is restarted.
- `assigned_by`
  - Reserved value `'governor'` for panes directly created by the governor.
  - LT-GOV slug for panes created on behalf of that LT-GOV.

Topology invariants:

- LT-GOV root pane: `parent_slug IS NULL`, `tier_id IS NOT NULL`, `assigned_by='governor'`.
- LT-GOV child pane: `parent_slug=<ltgov-slug>`, `tier_id=<same-tier>`, `epoch=<same-epoch>`, `assigned_by=<ltgov-slug>`.
- Direct worker: `parent_slug IS NULL`, `tier_id IS NULL`, `epoch=0`, `assigned_by='governor'`.

### `command_requests`

This is the brokered command queue.

```sql
CREATE TABLE IF NOT EXISTS command_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_slug TEXT NOT NULL,
    tier_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    request_type TEXT NOT NULL CHECK (
        request_type IN (
            'CreateWorker',
            'MergeWorker',
            'CloseWorker',
            'EscalateWorker',
            'RetryWorker',
            'SendMessage',
            'ReviewWorker'
        )
    ),
    params TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'executed')
    ) DEFAULT 'pending',
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    executed_at INTEGER,
    FOREIGN KEY (requester_slug) REFERENCES panes(slug)
);

CREATE INDEX IF NOT EXISTS idx_command_requests_status_created
  ON command_requests(status, created_at, id);

CREATE INDEX IF NOT EXISTS idx_command_requests_requester_epoch
  ON command_requests(requester_slug, tier_id, epoch, created_at);
```

Notes:

- `approved` is a claim state. The governor moves `pending -> approved` in a transaction before execution so a second poller cannot race the same row.
- `params` is JSON text. The governor validates shape per `request_type`.
- `executed_at` is set for both terminal queue outcomes: successful execution and explicit rejection.

### Append-only coordination tables

These replace `.dgov/progress/`, `.dgov/advisories/`, and `.dgov/attention/`.

```sql
CREATE TABLE IF NOT EXISTS progress_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    timestamp INTEGER NOT NULL DEFAULT (unixepoch()),
    message TEXT NOT NULL,
    phase TEXT NOT NULL,
    percent REAL,
    FOREIGN KEY (slug) REFERENCES panes(slug)
);

CREATE INDEX IF NOT EXISTS idx_progress_events_slug_id
  ON progress_events(slug, id DESC);


CREATE TABLE IF NOT EXISTS tier_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    timestamp INTEGER NOT NULL DEFAULT (unixepoch()),
    summary_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tier_summaries_tier_epoch_id
  ON tier_summaries(tier_id, epoch, id DESC);


CREATE TABLE IF NOT EXISTS advisories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_slug TEXT NOT NULL,
    to_slug TEXT NOT NULL,
    timestamp INTEGER NOT NULL DEFAULT (unixepoch()),
    advisory_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (from_slug) REFERENCES panes(slug),
    FOREIGN KEY (to_slug) REFERENCES panes(slug)
);

CREATE INDEX IF NOT EXISTS idx_advisories_to_slug_id
  ON advisories(to_slug, id DESC);

CREATE INDEX IF NOT EXISTS idx_advisories_from_slug_id
  ON advisories(from_slug, id DESC);
```

`advisories` subsumes both prior file channels:

- old `.dgov/advisories/*` becomes `advisory_type='advisory'`
- old `.dgov/attention/*` becomes `advisory_type='attention'`
- governor-generated review feedback can use `advisory_type='review_result'`

### Heartbeats

Use a dedicated append-only table. It keeps liveness logic simple and avoids overloading semantic event streams.

```sql
CREATE TABLE IF NOT EXISTS heartbeat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    tier_id TEXT,
    epoch INTEGER NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (slug) REFERENCES panes(slug)
);

CREATE INDEX IF NOT EXISTS idx_heartbeat_events_slug_id
  ON heartbeat_events(slug, id DESC);

CREATE INDEX IF NOT EXISTS idx_heartbeat_events_tier_epoch_id
  ON heartbeat_events(tier_id, epoch, id DESC);
```

## Typed Request Protocol

LT-GOVs write one row per requested action. They do not invoke the mutating CLI.

### Request payloads

#### `CreateWorker`

```json
{
  "slug": "fix-parser",
  "agent": "pi",
  "prompt": "Fix the parser bug in src/dgov/parser.py",
  "permission_mode": "acceptEdits",
  "extra_flags": "",
  "env": {
    "PYTEST_ADDOPTS": "-q"
  }
}
```

Governor-side rules:

- Ignore any `project_root` or `session_root` if present.
- Create child with:
  - `parent_slug=requester_slug`
  - `tier_id=request.tier_id`
  - `epoch=request.epoch`
  - `assigned_by=requester_slug`

#### `MergeWorker`

```json
{
  "target_slug": "fix-parser",
  "resolve": "skip",
  "squash": true
}
```

#### `CloseWorker`

```json
{
  "target_slug": "fix-parser",
  "force": false,
  "reason": "review-failed"
}
```

#### `EscalateWorker`

```json
{
  "target_slug": "fix-parser",
  "replacement_slug": "fix-parser-claude",
  "target_agent": "claude",
  "permission_mode": "acceptEdits",
  "prompt_override": "Retry the task with focus on parser edge cases."
}
```

`replacement_slug` is required so the LT-GOV can deterministically track the new pane without a separate result channel.

#### `RetryWorker`

```json
{
  "target_slug": "fix-parser",
  "replacement_slug": "fix-parser-retry1",
  "agent": "pi",
  "permission_mode": "acceptEdits",
  "prompt_override": "Retry with focus on bracket nesting."
}
```

#### `SendMessage`

```json
{
  "to_slug": "fix-parser",
  "advisory_type": "attention",
  "payload": {
    "message": "Avoid touching dashboard files.",
    "priority": "high"
  }
}
```

Governor behavior: insert a row into `advisories`.

#### `ReviewWorker`

```json
{
  "target_slug": "fix-parser",
  "full": false
}
```

Governor behavior:

1. Execute the existing review path.
2. Write a `review_result` advisory back to `requester_slug`.

Example advisory payload:

```json
{
  "target_slug": "fix-parser",
  "ok": false,
  "summary": "2 files changed; test file missing edge case",
  "files_changed": 2
}
```

## Wire Protocol

The LT-GOV writes directly to SQLite. That is the entire wire protocol.

The governor launches LT-GOV panes with:

- `DGOV_STATE_DB=/abs/path/.dgov/state.db`
- `DGOV_SLUG=<ltgov-slug>`
- `DGOV_TIER_ID=<tier-id>`
- `DGOV_TIER_EPOCH=<epoch>`

### Raw SQL

```sql
INSERT INTO command_requests (
    requester_slug,
    tier_id,
    epoch,
    request_type,
    params,
    status,
    created_at,
    executed_at
) VALUES (
    :requester_slug,
    :tier_id,
    :epoch,
    :request_type,
    :params,
    'pending',
    unixepoch(),
    NULL
);
```

### Python pseudocode

```python
import json
import os
import sqlite3


def submit_request(request_type: str, params: dict) -> None:
    db_path = os.environ["DGOV_STATE_DB"]
    requester_slug = os.environ["DGOV_SLUG"]
    tier_id = os.environ["DGOV_TIER_ID"]
    epoch = int(os.environ["DGOV_TIER_EPOCH"])

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        INSERT INTO command_requests (
            requester_slug, tier_id, epoch, request_type, params,
            status, created_at, executed_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', unixepoch(), NULL)
        """,
        (requester_slug, tier_id, epoch, request_type, json.dumps(params)),
    )
    conn.commit()
```

This helper is acceptable because it is not executing a mutating `dgov` action. It is only appending a request record. The authority check happens when the governor reads the row.

## Governor Broker Loop

### Polling and claiming

```python
def poll_command_queue(session_root: str) -> None:
    while True:
        rows = fetch_pending_requests(limit=50)
        for row in rows:
            if not claim_request(row["id"]):
                continue
            handle_request(row)
        reconcile_ltgov_heartbeats(session_root)
        time.sleep(1.0)
```

```python
def claim_request(request_id: int) -> bool:
    with db_transaction() as conn:
        cur = conn.execute(
            """
            UPDATE command_requests
            SET status = 'approved'
            WHERE id = ? AND status = 'pending'
            """,
            (request_id,),
        )
        return cur.rowcount == 1
```

`reject_request()` sets `status='rejected'`, stamps `executed_at=unixepoch()`, and emits a `ltgov_request_rejected` event containing the rejection reason. The reason is not stored in the queue row because the required queue schema stays minimal.

### Validation

```python
def validate_request(row: dict) -> tuple[bool, str | None, dict | None]:
    requester = get_pane(row["requester_slug"])
    if requester is None:
        return False, "unknown requester", None

    if requester["tier_id"] != row["tier_id"]:
        return False, "tier mismatch", None

    if requester["epoch"] != row["epoch"]:
        return False, "stale epoch", None

    if requester["parent_slug"] is not None:
        return False, "requester is not an lt-gov root", None

    if requester["state"] != "active":
        return False, "requester is not active", None

    params = json.loads(row["params"])
    target_slug = params.get("target_slug") or params.get("to_slug")
    if row["request_type"] == "CreateWorker":
        return True, None, params

    target = get_pane(target_slug)
    if target is None:
        return False, "unknown target pane", None

    if target["assigned_by"] != row["requester_slug"]:
        return False, "target not assigned to requester", None

    if target["tier_id"] != row["tier_id"]:
        return False, "target tier mismatch", None

    if target["epoch"] != row["epoch"]:
        return False, "target epoch mismatch", None

    return True, None, params
```

### Execution

```python
def handle_request(row: dict) -> None:
    ok, reason, params = validate_request(row)
    if not ok:
        reject_request(row["id"], reason)
        return

    try:
        match row["request_type"]:
            case "CreateWorker":
                create_worker_for_ltgov(row, params)
            case "MergeWorker":
                merge_worker_for_ltgov(row, params)
            case "CloseWorker":
                close_worker_for_ltgov(row, params)
            case "EscalateWorker":
                escalate_worker_for_ltgov(row, params)
            case "RetryWorker":
                retry_worker_for_ltgov(row, params)
            case "SendMessage":
                send_message_for_ltgov(row, params)
            case "ReviewWorker":
                review_worker_for_ltgov(row, params)
            case _:
                raise ValueError(f"unknown request type: {row['request_type']}")
    except Exception as exc:
        reject_request(row["id"], str(exc))
        emit_event("ltgov_request_rejected", row["requester_slug"], request_id=row["id"])
        raise
    else:
        execute_request(row["id"])
        emit_event("ltgov_request_executed", row["requester_slug"], request_id=row["id"])
```

Rules:

- The governor resolves all filesystem roots from the requester pane row, never from request params.
- The governor uses internal functions like `create_worker_pane()`, `merge_worker_pane()`, `close_worker_pane()`, and `review_worker_pane()`.
- `dgov pane util` is never exposed through the broker.

## Scope Enforcement

The scope rule from the review becomes a concrete predicate:

```text
An LT-GOV request may act only on panes where:
  target.assigned_by == requester.slug
  AND target.tier_id == requester.tier_id
  AND target.epoch == requester.epoch
```

Implications:

- LT-GOVs cannot act on siblings in another tier.
- LT-GOVs cannot act on direct governor workers.
- LT-GOVs cannot act on children created by a previous epoch of the same tier.
- A restarted LT-GOV cannot silently inherit old workers.

## Append-Only Coordination State

### Progress

Workers and LT-GOVs append progress instead of replacing `progress/<slug>.json`.

Example write:

```python
def append_progress(slug: str, message: str, phase: str, percent: float | None) -> None:
    conn.execute(
        """
        INSERT INTO progress_events (slug, message, phase, percent)
        VALUES (?, ?, ?, ?)
        """,
        (slug, message, phase, percent),
    )
```

Dashboard query:

```sql
SELECT p.slug, p.message, p.phase, p.percent, p.timestamp
FROM progress_events AS p
JOIN (
    SELECT slug, MAX(id) AS max_id
    FROM progress_events
    GROUP BY slug
) AS latest
  ON latest.max_id = p.id;
```

### Tier summaries

LT-GOVs append summary snapshots. The dashboard reads the latest row for `(tier_id, epoch)`.

Example `summary_json`:

```json
{
  "done": 3,
  "active": 2,
  "blocked": 1,
  "reviewing": 1,
  "eta_minutes": 4,
  "notes": "parser tier waiting on retry"
}
```

### Advisories and attention

All side-channel communication is append-only.

Examples:

- worker -> LT-GOV: `advisory_type='advisory'`
- governor -> worker: `advisory_type='attention'`
- governor -> LT-GOV after review: `advisory_type='review_result'`

The dashboard reads the latest advisory per `(to_slug, advisory_type)` when it needs a current view. Nothing ever overwrites prior rows.

## Leases and Heartbeats

### Emission

Every LT-GOV appends one heartbeat every 30 seconds:

```python
def emit_heartbeat() -> None:
    conn.execute(
        """
        INSERT INTO heartbeat_events (slug, tier_id, epoch)
        VALUES (?, ?, ?)
        """,
        (
            os.environ["DGOV_SLUG"],
            os.environ["DGOV_TIER_ID"],
            int(os.environ["DGOV_TIER_EPOCH"]),
        ),
    )
```

### Reconciliation

The governor checks liveness once per poll cycle.

```python
def reconcile_ltgov_heartbeats(session_root: str) -> None:
    for ltgov in list_active_ltgov_roots(session_root):
        last = latest_heartbeat_ts(ltgov["slug"])
        if last is None or now() - last <= 90:
            continue
        mark_ltgov_suspected_dead(ltgov)
        quarantine_children(ltgov)
```

### Suspected-dead handling

When a LT-GOV misses heartbeats for more than 90 seconds:

1. Emit a `ltgov_suspected_dead` event.
2. Mark the LT-GOV as suspected dead in durable state by appending that liveness event.
3. Quarantine its active children by setting their pane `state='orphaned'`.
4. Emit `pane_orphaned` events for each quarantined child.
5. Stop automatic queue execution for requests from that LT-GOV.
6. Wait for an explicit governor action:
   - adopt child
   - restart tier with new epoch
   - abandon child

### State model for quarantine

`panes.state` must add:

- `orphaned`

Recommended additional liveness marker:

- store LT-GOV liveness as the latest event type, not as another pane state for workers
- governor dashboard treats any LT-GOV with latest liveness event `ltgov_suspected_dead` and no newer heartbeat as suspected dead

This avoids overloading worker lifecycle state with LT-GOV liveness. The child quarantine itself is still materialized in `panes.state='orphaned'`.

## Crash Recovery and Epoch Rules

### Tier restart

Restart means:

1. Governor chooses a tier root slug, for example `ltgov-tier-1`.
2. Governor increments the tier epoch from `N` to `N+1`.
3. Governor relaunches the LT-GOV root pane with the same `tier_id` and new `epoch`.
4. Governor launches it with `DGOV_TIER_EPOCH=N+1`.

### Hard rule: no implicit adoption across epochs

Old-epoch children stay in the old epoch. They are not rebound automatically.

Example:

- old LT-GOV root: `tier_id='tier-1', epoch=2`
- child A: `tier_id='tier-1', epoch=2, state='orphaned'`
- restarted LT-GOV root: `tier_id='tier-1', epoch=3`

The restarted LT-GOV cannot request actions on child A until the governor explicitly adopts it by updating:

```sql
UPDATE panes
SET parent_slug = :new_ltgov_slug,
    epoch = :new_epoch,
    assigned_by = :new_ltgov_slug,
    state = 'active'
WHERE slug = :child_slug
  AND state = 'orphaned';
```

If the governor chooses to abandon instead:

```sql
UPDATE panes
SET state = 'abandoned'
WHERE slug = :child_slug
  AND state = 'orphaned';
```

### Stale queue entries

Any queued row with `request.epoch != requester.epoch` is auto-rejected.

Rejection reasons should be explicit:

- `stale epoch`
- `target epoch mismatch`
- `requester suspected dead`
- `target not assigned to requester`

### Dashboard behavior

The dashboard must render orphaned workers distinctly:

- state label: `orphaned`
- tier grouping: show under an `Orphaned (epoch N)` subsection if their LT-GOV is dead or restarted
- action affordances: only governor actions, no LT-GOV-originated automation

## Required CLI and Lifecycle Changes

### `pane create`

Governor-created direct workers continue to work.

Governor-created LT-GOV root creation must set:

- `tier_id`
- `epoch`
- `assigned_by='governor'`
- `parent_slug=NULL`

Governor-created child creation on behalf of LT-GOV must set:

- `parent_slug=<requester_slug>`
- `tier_id=<request.tier_id>`
- `epoch=<request.epoch>`
- `assigned_by=<requester_slug>`

### Governor-only mutating commands

Mutating `dgov` pane commands should fail fast when executed from a worker or LT-GOV worktree:

- `pane create`
- `pane merge`
- `pane close`
- `pane retry`
- `pane escalate`
- `pane review`
- `pane util`

Read-only commands can remain available:

- `pane list`
- `pane capture`
- `pane diff`

This does two things:

1. The documented LT-GOV path uses the queue, not the CLI.
2. A prompt-injected LT-GOV cannot turn itself into a second unrestricted governor just by trying the CLI.

## Migration Plan

### Phase A: schema migration

Run once against existing `state.db`:

```sql
ALTER TABLE panes ADD COLUMN parent_slug TEXT;
ALTER TABLE panes ADD COLUMN tier_id TEXT;
ALTER TABLE panes ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE panes ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'governor';

CREATE TABLE IF NOT EXISTS command_requests (...);
CREATE TABLE IF NOT EXISTS progress_events (...);
CREATE TABLE IF NOT EXISTS tier_summaries (...);
CREATE TABLE IF NOT EXISTS advisories (...);
CREATE TABLE IF NOT EXISTS heartbeat_events (...);
CREATE INDEX IF NOT EXISTS ...;
```

### Phase B: backfill existing panes

Existing panes are plain governor-managed workers:

```sql
UPDATE panes
SET parent_slug = NULL
WHERE parent_slug IS NULL;

UPDATE panes
SET tier_id = NULL
WHERE tier_id IS NULL;

UPDATE panes
SET epoch = 0
WHERE epoch IS NULL;

UPDATE panes
SET assigned_by = 'governor'
WHERE assigned_by IS NULL;
```

### Phase C: stop writing file channels

Replace writes to:

- `.dgov/progress/*`
- `.dgov/advisories/*`
- `.dgov/attention/*`

with inserts into:

- `progress_events`
- `tier_summaries`
- `advisories`

The dashboard should read SQL first. Temporary compatibility mode can read old files only if the new tables are empty. Remove that fallback after one release.

### Phase D: broker enforcement

1. Remove any LT-GOV prompt instructions that tell it to call `dgov pane ...`.
2. Add LT-GOV helper code that writes `command_requests`.
3. Make mutating CLI commands reject worker-context execution.
4. Start the governor poll loop.

## Performance

### Queue polling at 1 Hz

Idle poll query:

```sql
SELECT id, requester_slug, tier_id, epoch, request_type, params, status, created_at
FROM command_requests
WHERE status = 'pending'
ORDER BY created_at, id
LIMIT 50;
```

With `idx_command_requests_status_created`, the steady-state cost is small:

- 1 indexed read per second
- 86,400 polls per day
- typically zero returned rows when idle
- no table rewrites, only occasional append and status update when work exists

Expected overhead on local SQLite WAL:

- CPU: negligible relative to tmux, git, and agent subprocesses
- I/O: effectively page-cache hits after warmup
- lock contention: low, because the hot path is append-only inserts plus one governor status update per handled request

The real contention source in the current system is not an idle 1 Hz poll. It is concurrent read-modify-replace updates and many actors trying to execute mutating operations. This design removes most of that pressure by centralizing mutation in the governor.

### Heartbeat overhead

At 30-second heartbeats:

- 3 LT-GOVs => 6 heartbeat inserts per minute
- 10 LT-GOVs => 20 heartbeat inserts per minute

That is trivial for SQLite WAL, especially because each row is append-only and indexed by `slug`.

## Security Model

### What a compromised LT-GOV can still do

1. Spam the queue with invalid requests.
2. Spam progress, summary, advisory, and heartbeat tables.
3. Read repo contents and any DB rows already visible to the same user.
4. Continue editing files in its own worktree.

### What a compromised LT-GOV cannot do through the broker

1. Create panes outside its tier.
2. Merge, close, retry, escalate, or review panes outside `assigned_by=<its-slug>` and the current epoch.
3. Reanimate stale children from an old epoch by replaying old queue rows.
4. Execute arbitrary `dgov pane util` commands, because that request type does not exist.
5. Smuggle a different `project_root` or `session_root` through `params`, because the governor ignores those fields.

### Boundary statement

This is an authority boundary inside the `dgov` control plane, not a hostile same-user sandbox. If a LT-GOV can arbitrarily rewrite `.dgov/state.db` or invoke tmux and git directly as the same OS user, SQLite validation alone cannot stop it. That stronger boundary requires separate users, file permissions, or a broker service process with tighter IPC. This design still fixes the original flaw: prompt following is no longer treated as authorization.

## Backward Compatibility

Plain workers still work exactly as today:

- governor uses `pane create`, `pane wait`, `pane review`, `pane merge`, `pane close`
- direct workers keep `tier_id=NULL`, `epoch=0`, `parent_slug=NULL`, `assigned_by='governor'`
- dashboard flat mode remains the default for panes without `tier_id`

No plain worker has to know that LT-GOV queueing exists.

The LT-GOV path is additive:

- only LT-GOV roots get `tier_id` and `epoch > 0`
- only LT-GOV roots emit tier summaries and heartbeats
- only LT-GOV roots write `command_requests`

## Recommended Implementation Order

1. Add durable topology columns to `panes`.
2. Add append-only coordination tables and switch dashboard reads.
3. Add `command_requests` plus governor poll loop.
4. Gate mutating CLI commands from worker context.
5. Add LT-GOV heartbeat emission and orphan reconciliation.
6. Add tier restart and explicit adoption flows.

## Bottom Line

The LT-GOV should be a scoped planner with a write-only request channel, not a second governor with ambient CLI authority. The combination of brokered requests, durable tier identity, append-only coordination records, heartbeat-based reconciliation, and epoch-based restart rules closes the five architectural gaps from the prior review without inventing a second control plane.
