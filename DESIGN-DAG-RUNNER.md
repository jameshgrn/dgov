# DAG Runner Design

## Status

Proposed governor-side DAG execution engine for `dgov`.

This is not an LT-GOV. It is an in-process governor feature that reads a machine-readable DAG file, launches worker panes through the existing Python APIs, and persists enough run state to resume after governor death.

## Design Goals

1. Keep one authority boundary: only the governor process mutates pane state.
2. Reuse existing primitives instead of shelling out to `dgov pane ...`.
3. Serialize tasks by both explicit dependencies and file overlap.
4. Make retries and escalations explicit and durable.
5. Stop cleanly on merge conflicts instead of guessing through them.
6. Emit enough structured events for the dashboard to render DAG progress.

## Non-Goals

1. This is not the brokered LT-GOV queue. No `command_requests` rows are written in this design.
2. This is not a distributed scheduler. One governor process owns one DAG run.
3. This is not speculative generalized workflow orchestration. It only covers the pane lifecycle that already exists: create, wait, review, retry, escalate, merge, close.

## 1. DAG Format

### Choice: TOML

Use TOML, not YAML.

Reasons:

1. `dgov` already has a TOML-based batch spec in [`src/dgov/batch.py`](src/dgov/batch.py), so TOML fits the current codebase and operator workflow.
2. Python 3.12 ships `tomllib`, so parsing needs no new runtime dependency.
3. The shape here is mostly scalars, lists, and keyed task blocks. TOML handles that cleanly without YAML's implicit typing footguns.
4. Comments matter for hand-authored DAGs. TOML comments are enough without pulling in a full schema layer.

### Format Rules

1. Paths are project-root-relative.
2. No globbing in file specs. The scheduler needs deterministic overlap checks.
3. Task slugs are the task identity for the DAG. Pane slugs may change across retries or escalations.
4. Agent escalation is an ordered chain. The runner always starts with `agent`, then tries each entry in `escalation` in order.
5. `files.create`, `files.edit`, and `files.delete` are advisory for scheduling and review scope. They do not hard-block the agent from touching other files; review still catches drift.

### Schema

```toml
[dag]
version = 1
name = "dag-runner"
project_root = "."
session_root = "."
default_permission_mode = "acceptEdits"
default_timeout_s = 900
default_max_retries = 1
merge_resolve = "skip"
merge_squash = true

[tasks.<slug>]
summary = "Short operator-readable description"
agent = "hunter"
escalation = ["gemini", "claude"]
depends_on = ["other-slug"]
prompt = """
Concrete worker prompt.
"""
commit_message = "Imperative commit message"
permission_mode = "acceptEdits"
timeout_s = 900

[tasks.<slug>.files]
create = ["src/dgov/dag.py"]
edit = ["src/dgov/batch.py"]
delete = []
```

### Required Fields

Global:

1. `dag.version`
2. `dag.name`
3. `tasks`

Per task:

1. `summary`
2. `agent`
3. `prompt`
4. `commit_message`
5. `files.create` or `files.edit` or `files.delete`

### Optional Fields

Global:

1. `dag.project_root`
2. `dag.session_root`
3. `dag.default_permission_mode`
4. `dag.default_timeout_s`
5. `dag.default_max_retries`
6. `dag.merge_resolve`
7. `dag.merge_squash`

Per task:

1. `escalation`
2. `depends_on`
3. `permission_mode`
4. `timeout_s`

### Derived Fields

The engine derives:

1. `touches`: normalized union of `files.create`, `files.edit`, and `files.delete`
2. `agent_chain`: `[agent] + escalation`
3. `topological_index`: canonical dependency order
4. `tier_index`: dependency order plus file-overlap serialization

### Example

```toml
[dag]
version = 1
name = "dag-runner"
project_root = "."
default_permission_mode = "acceptEdits"
default_timeout_s = 900
default_max_retries = 1
merge_resolve = "skip"
merge_squash = true

[tasks.T0]
summary = "Parse TOML DAG files into dataclasses"
agent = "hunter"
escalation = ["gemini", "claude"]
depends_on = []
prompt = """
Implement TOML parsing for DAG files in src/dgov/dag.py.
Validate required fields and normalize file specs.
"""
commit_message = "Parse TOML DAG specs"
timeout_s = 600

[tasks.T0.files]
create = ["src/dgov/dag.py"]
edit = []
delete = []

[tasks.T1]
summary = "Compute topological order and execution tiers"
agent = "hunter"
escalation = ["gemini", "claude"]
depends_on = ["T0"]
prompt = """
Implement DAG validation, topological sort, and tier computation.
Reuse the existing overlap rule from batch.py.
"""
commit_message = "Add DAG tier computation"

[tasks.T1.files]
create = []
edit = ["src/dgov/dag.py", "src/dgov/batch.py"]
delete = []
```

## 2. Execution Engine

### Module

Create `src/dgov/dag.py`.

This module owns:

1. DAG parsing and validation
2. Topological ordering and tier computation
3. Run orchestration
4. Retry and escalation state transitions
5. DAG run summaries

SQLite DDL and row helpers belong in `src/dgov/persistence.py`, because that module already owns `state.db`.

### Core Dataclasses

```python
@dataclass(frozen=True)
class DagFileSpec:
    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagTaskSpec:
    slug: str
    summary: str
    prompt: str
    commit_message: str
    agent: str
    escalation: tuple[str, ...]
    depends_on: tuple[str, ...]
    files: DagFileSpec
    permission_mode: str
    timeout_s: int


@dataclass(frozen=True)
class DagDefinition:
    name: str
    dag_file: str
    project_root: str
    session_root: str
    default_max_retries: int
    merge_resolve: str
    merge_squash: bool
    tasks: dict[str, DagTaskSpec]


@dataclass(frozen=True)
class DagRunOptions:
    dry_run: bool = False
    tier_limit: int | None = None
    skip: frozenset[str] = frozenset()
    max_retries: int = 1
    auto_merge: bool = True


@dataclass
class DagRunSummary:
    run_id: int
    dag_file: str
    status: str
    succeeded: list[str]
    failed: list[str]
    skipped: list[str]
    escalated: list[dict[str, object]]
    merged: list[str]
    unmerged: list[str]
```

### Public API

```python
def parse_dag_file(path: str) -> DagDefinition: ...
def validate_dag(tasks: dict[str, DagTaskSpec]) -> None: ...
def topological_order(tasks: dict[str, DagTaskSpec]) -> list[str]: ...
def compute_tiers(tasks: dict[str, DagTaskSpec]) -> list[list[str]]: ...
def run_dag(
    dag_file: str,
    *,
    dry_run: bool = False,
    tier_limit: int | None = None,
    skip: set[str] | None = None,
    max_retries: int = 1,
    auto_merge: bool = True,
) -> DagRunSummary: ...
```

### Scheduling Rules

The runner computes a canonical topological order first, then groups tasks into tiers using two conditions:

1. All `depends_on` tasks are already in earlier tiers.
2. The task's file specs do not overlap with any task already placed in the same tier.

Overlap rule:

1. Exact path match conflicts.
2. Ancestor and descendant conflict. `src/dgov/` conflicts with `src/dgov/dag.py`.
3. No glob expansion.

This matches the intent already present in `batch.py`. The right implementation path is to move the pure DAG helpers into `src/dgov/dag.py` and have `batch.py` import them, not keep two schedulers drifting apart.

### Governor-Side Call Path

The DAG runner calls these functions directly:

1. `create_worker_pane(...)`
2. `wait_worker_pane(..., auto_retry=False)`
3. `review_worker_pane(...)`
4. `retry_worker_pane(...)`
5. `escalate_worker_pane(...)`
6. `merge_worker_pane(...)`
7. `close_worker_pane(...)`

It never shells out to `dgov pane ...`.

The `auto_retry=False` point matters. `wait_worker_pane()` already knows how to auto-retry on its own, but a DAG runner cannot let that happen invisibly because the DAG layer must persist attempt numbers, current agent, current pane slug, and escalation history explicitly.

### Execution Loop

For each tier:

1. Mark the tier as started in SQLite and emit `dag_tier_started`.
2. Dispatch every ready task in the tier.
3. Wait for all currently active panes in the tier to finish.
4. Review each completed task.
5. For tasks that fail review or execution, drive retry or escalation until each task reaches one of:
   - `reviewed_pass`
   - `failed`
   - `skipped`
6. If `auto_merge=True`, merge all `reviewed_pass` tasks in canonical topological order.
7. If any merge returns conflict or error, stop the whole DAG immediately.
8. Mark the tier completed.

The runner only advances to the next tier after every task in the current tier is terminal.

### Attempt State Machine

Per task, the DAG runner owns a stable task slug and a mutable pane slug.

States:

1. `pending`
2. `dispatched`
3. `waiting`
4. `reviewing`
5. `retrying`
6. `escalating`
7. `reviewed_pass`
8. `merged`
9. `failed`
10. `skipped`

Suggested implementation helpers:

```python
def dispatch_task_attempt(...) -> DagTaskRuntime: ...
def wait_for_tier_attempts(...) -> dict[str, dict]: ...
def review_task_attempt(...) -> dict: ...
def retry_task_attempt(...) -> DagTaskRuntime: ...
def escalate_task_attempt(...) -> DagTaskRuntime | None: ...
def merge_ready_tasks(...) -> list[str]: ...
```

### Review Logic

Use `review_worker_pane(project_root, pane_slug, session_root=..., full=False)`.

Interpretation:

1. `commit_count == 0`: immediate escalation. Do not waste the retry budget on an attempt that produced nothing.
2. `verdict == "safe"`: mark pane `reviewed_pass`, emit `dag_task_completed`, and queue for merge.
3. `verdict != "safe"`: retry the same agent up to `max_retries`, then escalate.

`review_worker_pane()` emits `review_pass` and `review_fail` events already, but it does not update pane state. The DAG runner should call `update_pane_state()` to move the pane into `reviewed_pass` or `reviewed_fail` so the pane lifecycle remains consistent with the existing state machine.

### Runtime Failure Logic

The source APIs imply one extra rule that is worth making explicit:

1. `create_worker_pane()` can fail before the pane even exists, typically because of health checks or concurrency guards.
2. `wait_worker_pane()` can return with the pane in `failed`, `abandoned`, or `timed_out`.

Recommended policy:

1. Health-check or creation failure: skip directly to the next agent in the escalation chain.
2. Timeout: escalate immediately.
3. `failed` or `abandoned`: retry on the same agent once, then escalate.

That is the only coherent way to use both `retry_worker_pane()` and `escalate_worker_pane()` without making the task layer lie about attempts.

### 0-Commit Detection

Use `review_worker_pane()` as the canonical detector.

It already reports:

1. `commit_count`
2. `issues`
3. `verdict`

The DAG runner should branch on `commit_count == 0`, not on the human-readable issue string.

### Merge Logic

Merge in canonical topological order, never in worker completion order.

That means:

1. A task never merges before any of its dependencies.
2. Sibling tasks in the same tier merge in stable slug order derived from the topological sort.

Use:

```python
merge_worker_pane(
    project_root,
    pane_slug,
    session_root=session_root,
    resolve=dag.merge_resolve,
    squash=dag.merge_squash,
)
```

Important detail from the current implementation:

`merge_worker_pane()` already performs cleanup on successful merge by removing the pane record and worktree. The DAG runner should not issue a second unconditional `close_worker_pane()` after successful merge. It only needs explicit close calls for panes that remain open after failure, retry supersession, or `--no-auto-merge`.

### Final Summary

`run_dag()` should return a summary with:

1. `succeeded`
2. `failed`
3. `skipped`
4. `escalated`
5. `merged`
6. `unmerged`
7. `status`

Example:

```json
{
  "run_id": 12,
  "status": "completed",
  "succeeded": ["T0", "T1", "T2"],
  "failed": ["T5"],
  "skipped": ["T8"],
  "escalated": [
    {"slug": "T2", "from": "hunter", "to": "gemini", "reason": "zero_commit"}
  ],
  "merged": ["T0", "T1", "T2"],
  "unmerged": ["T5", "T8"]
}
```

## 3. CLI Command

Add:

```text
dgov dag run <dagfile> [options]
```

Suggested implementation: `src/dgov/cli/dag_cmd.py`, then `cli.add_command(dag)` in `src/dgov/cli/__init__.py`.

### Options

1. `--dry-run`
2. `--tier N`
3. `--skip <slug>` repeatable
4. `--max-retries N` with default `1`
5. `--auto-merge/--no-auto-merge`

### Semantics

1. `--dry-run`
   - Parse and validate the DAG
   - Compute tiers
   - Print execution order
   - Do not create panes or write DAG rows
2. `--tier N`
   - Run only tiers `0..N`, inclusive
   - Use zero-based tier indices
3. `--skip <slug>`
   - Mark the task skipped before scheduling
   - Transitively skip its dependents
4. `--max-retries N`
   - Overrides `dag.default_max_retries`
5. `--no-auto-merge`
   - Run through wait and review
   - Leave `reviewed_pass` panes open and unmerged
   - Persist the DAG run as `awaiting_merge` instead of `completed`

### Resume Behavior

There is no separate `resume` command in this first pass.

Instead:

1. `dgov dag run <dagfile>` checks for an unfinished DAG run for the same absolute DAG path.
2. If found, it validates that the DAG file hash matches the stored hash.
3. If the hash matches, it resumes from SQLite state.
4. If the hash differs, it refuses to resume and asks for operator intervention.

That prevents duplicate concurrent runs of the same DAG and avoids resuming against a modified spec.

## 4. Governor Integration

The DAG runner lives in the governor process.

It calls internal Python functions directly. No shelling out. No subprocess recursion into the CLI. That keeps one authority boundary and aligns with the brokered LT-GOV design's core rule: mutating `dgov` actions stay inside trusted governor code.

## 5. Failure Modes

### Task Produces 0 Commits

1. Detected via `review_worker_pane().commit_count == 0`
2. Emit `dag_task_escalated`
3. Escalate immediately to the next agent in chain
4. If the chain is exhausted, mark the task `failed`

### Review Verdict Is Not `safe`

1. Mark the current pane `reviewed_fail`
2. Retry the same agent up to `max_retries`
3. If retries are exhausted, escalate
4. If the chain is exhausted, mark the task `failed`

### Merge Conflict

1. `merge_worker_pane(..., resolve="skip")` returns `{"error": ..., "conflicts": ...}`
2. Emit `dag_failed`
3. Mark the DAG run `failed`
4. Stop immediately
5. Report which tasks merged and which did not

This is the right stopping point. Conflict auto-resolution is a different workflow and should not be hidden inside the first DAG runner.

### Agent Health Check Fails

1. `create_worker_pane()` raises before or during launch
2. Record the failure on the DAG task row
3. Skip directly to the next agent in the escalation chain
4. If no agents remain, mark the task `failed`

### Timeout

1. `wait_worker_pane()` raises `PaneTimeoutError`
2. The pane state is already moved to `timed_out`
3. Emit `dag_task_escalated`
4. Escalate to the next agent in chain

### All Agents in Chain Fail

1. Mark the task `failed`
2. Emit `dag_task_failed`
3. Continue the DAG only for tasks that do not depend on the failed task
4. Transitively skip dependents

## 6. DAG State Persistence

Use the existing `state.db`.

Add two tables:

```sql
CREATE TABLE IF NOT EXISTS dag_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_file TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_tier INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dag_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    pane_slug TEXT,
    error TEXT,
    UNIQUE(dag_run_id, slug),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
);
```

### `state_json` Contents

Store the mutable run summary that is awkward to query column-by-column:

```json
{
  "dag_name": "dag-runner",
  "dag_sha256": "8f4f...",
  "options": {
    "tier_limit": null,
    "skip": [],
    "max_retries": 1,
    "auto_merge": true
  },
  "tiers": [["T0", "T5"], ["T1", "T6"], ["T2"]],
  "topological_order": ["T0", "T5", "T1", "T6", "T2"],
  "completed": ["T0"],
  "failed": [],
  "skipped": [],
  "merged": [],
  "escalations": []
}
```

### Persistence Helpers

Add public helpers in `src/dgov/persistence.py`:

```python
def ensure_dag_tables(session_root: str) -> None: ...
def create_dag_run(session_root: str, dag_file: str, state_json: dict) -> int: ...
def get_open_dag_run(session_root: str, dag_file: str) -> dict | None: ...
def update_dag_run(
    session_root: str,
    dag_run_id: int,
    *,
    status: str | None = None,
    current_tier: int | None = None,
    state_json: dict | None = None,
) -> None: ...
def upsert_dag_task(...): ...
def list_dag_tasks(session_root: str, dag_run_id: int) -> list[dict]: ...
```

## 7. Observability

Extend `VALID_EVENTS` and emit:

1. `dag_started`
2. `dag_tier_started`
3. `dag_task_dispatched`
4. `dag_task_completed`
5. `dag_task_failed`
6. `dag_task_escalated`
7. `dag_tier_completed`
8. `dag_completed`
9. `dag_failed`

### Event Payload Shape

Run-level events:

```json
{
  "dag_run_id": 12,
  "dag_file": "/abs/path/to/dag.toml",
  "tier": 2
}
```

Task-level events:

```json
{
  "dag_run_id": 12,
  "tier": 2,
  "task_slug": "T3",
  "pane_slug": "T3-esc-1",
  "agent": "gemini",
  "attempt": 2,
  "reason": "zero_commit"
}
```

### Dashboard Impact

Minimal by design.

The current dashboard event feed already reads generic events through `read_events()`. The only required change is to allow these event names through `VALID_EVENTS`. A dashboard-specific parser can come later if operators want DAG-specific grouping, but it is not required for the first runner.

## 8. Relationship To LT-GOV

The DAG runner is governor-side orchestration, not an LT-GOV.

It shares the same authority model as the broker design:

1. The governor executes mutating actions.
2. Workers do not.

Future path, explicitly out of scope:

1. An LT-GOV could write a `RunDag` request into `command_requests`.
2. The governor could approve it and call `run_dag(...)`.

That would reuse this module. It does not change the first implementation.
