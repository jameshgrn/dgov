# DAG Runner Implementation DAG

Scope: implement a governor-side DAG runner in `dgov`, backed by TOML specs, SQLite resume state, explicit retry/escalation handling, and event emission that the existing dashboard can already surface.

This DAG assumes the runner reuses the pure DAG helpers already prototyped in `batch.py` instead of maintaining two independent schedulers.

## Execution Rules

1. Default worker: `hunter`
2. Escalation chain for every task unless stated otherwise: `gemini`, then `claude`
3. Merge policy: `resolve="skip"`, `squash=True`
4. Auto-retry inside `wait_worker_pane()` must be disabled. The DAG runner owns retries itself.
5. Stop the whole DAG on merge conflict. Skip only transitive dependents for task failures.

## Dependency Graph

```text
TIER 0
  ├─ T0: DAG file parser (TOML -> dataclasses)
  └─ T5: DAG state persistence (SQLite tables + helpers)

TIER 1
  ├─ T1: Topological sort + tier computation        (depends on T0)
  ├─ T6: Event emission + dashboard integration     (depends on T5)
  └─ T7: Dashboard DAG fixture in TOML              (depends on T0)

TIER 2
  └─ T2a: Single-tier execution loop                (depends on T1, T5, T6)

TIER 3
  ├─ T2b: Multi-tier orchestration + options         (depends on T2a)
  └─ T3: Escalation + retry logic                   (depends on T2a)

TIER 4
  └─ T4: CLI command (run + merge)                   (depends on T2b, T3)

TIER 5
  └─ T8: Integration tests                          (depends on T2a, T2b, T3, T4, T5, T6, T7)
```

## Merge Order

```text
T0 -> T5 -> T1 -> T6 -> T7 -> T2a -> T2b -> T3 -> T4 -> T8
```

Reasoning:

1. `T0`, `T1`, `T2a`, `T2b`, and `T3` all touch `src/dgov/dag.py`, so they must merge in strict order.
2. `T5` and `T6` both touch `src/dgov/persistence.py`, so they must merge in strict order.
3. `T2b` and `T3` can run in parallel (same tier) because T2b touches only the multi-tier entry point while T3 touches only the escalation helpers — but both edit `dag.py`, so they merge sequentially.
4. `T8` comes last because it needs the real runner surface, the CLI, and the fixture.

## Task Specifications

### T0: DAG file parser

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py` (new)
  - `tests/test_dag.py` (new)
- Depends on: none
- Spec:
  1. Create `src/dgov/dag.py`.
  2. Add dataclasses:
     - `DagFileSpec`
     - `DagTaskSpec`
     - `DagDefinition`
     - `DagRunOptions`
     - `DagRunSummary`
  3. Implement:
     - `parse_dag_file(path: str) -> DagDefinition`
     - `_parse_task(slug: str, raw: dict, defaults: dict, dag_file: str, project_root: str, session_root: str) -> DagTaskSpec`
     - `_normalize_file_specs(project_root: str, files: dict) -> DagFileSpec`
  4. Parse TOML with `tomllib`.
  5. Validate:
     - unique task slugs
     - non-empty `prompt`
     - non-empty `commit_message`
     - `agent` present
     - at least one touched file across create/edit/delete
     - no globs in file specs
     - all file paths are relative
  6. Normalize file lists into tuples and sort them for deterministic tiering.
  7. Add parser tests for valid input, missing required fields, empty prompt, and illegal file specs.
  8. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
     - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Parse TOML DAG specs`

### T1: Topological sort + tier computation

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `src/dgov/batch.py`
  - `tests/test_dag.py`
  - `tests/test_batch_dag.py`
- Depends on: `T0`
- Spec:
  1. Implement pure DAG helpers in `src/dgov/dag.py`:
     - `validate_dag(tasks: dict[str, DagTaskSpec]) -> None`
     - `topological_order(tasks: dict[str, DagTaskSpec]) -> list[str]`
     - `compute_tiers(tasks: dict[str, DagTaskSpec]) -> list[list[str]]`
     - `transitive_dependents(tasks: dict[str, DagTaskSpec], failed: set[str]) -> set[str]`
     - `render_dry_run(order: list[str], tiers: list[list[str]]) -> str`
  2. Reuse the current overlap rule from `batch.py`: exact match or ancestor/descendant path means conflict.
  3. Update `src/dgov/batch.py` to import these helpers instead of keeping a second copy.
  4. Preserve the existing `batch` behavior and tests while switching it to the shared DAG code.
  5. Add tests for:
     - missing deps
     - self-cycle
     - multi-node cycle
     - diamond graph
     - overlap serialization
     - stable topological order
  6. Run:
     - `uv run pytest tests/test_dag.py tests/test_batch_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py src/dgov/batch.py tests/test_dag.py tests/test_batch_dag.py`
     - `uv run ruff format src/dgov/dag.py src/dgov/batch.py tests/test_dag.py tests/test_batch_dag.py`
- Commit: `Share DAG ordering helpers`

### T2a: Single-tier execution loop

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `tests/test_dag.py`
- Depends on: `T1`, `T5`, `T6`
- Spec:
  1. Implement single-tier helpers:
     - `_dispatch_task(dag: DagDefinition, task: DagTaskSpec, run_id: int, session_root: str) -> dict`
     - `_wait_for_tier(dag: DagDefinition, active_panes: dict, session_root: str) -> dict[str, dict]`
     - `_review_task(dag: DagDefinition, task_slug: str, pane_slug: str, session_root: str) -> dict`
     - `_merge_tasks_in_order(dag: DagDefinition, ready: list[str], pane_slugs: dict, session_root: str) -> list[str]`
     - `_run_single_tier(dag: DagDefinition, tier: list[str], run_id: int, task_states: dict, options: DagRunOptions, session_root: str) -> dict`
  2. Use the governor-side Python APIs directly:
     - `create_worker_pane`
     - `wait_worker_pane(..., auto_retry=False, timeout=task_spec.timeout_s)`
     - `review_worker_pane`
     - `merge_worker_pane`
  3. `_run_single_tier` orchestrates one tier:
     - dispatch all ready tasks
     - wait for all active panes
     - only review panes in `done` state (skip `failed`/`timed_out`/`abandoned`)
     - merge reviewed-pass tasks in canonical topological order if `auto_merge=True`
  4. Update pane state explicitly with `update_pane_state()`:
     - `reviewed_pass`
     - `reviewed_fail`
  5. On successful merge, do NOT call `close_worker_pane()` again. `merge_worker_pane()` already removes the pane and worktree on success.
  6. If `merge_worker_pane()` returns a dict with an `error` key (conflict or otherwise), stop the tier immediately and return partial results.
  7. Add tests for:
     - successful single-tier dispatch+wait+review+merge
     - merge error stops tier
     - review skipped for failed/timed_out panes
  8. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
     - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Add single-tier DAG execution`

### T2b: Multi-tier orchestration + options

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `tests/test_dag.py`
- Depends on: `T2a`
- Spec:
  1. Implement the public entry point:
     - `run_dag(dag_file: str, *, dry_run: bool = False, tier_limit: int | None = None, skip: set[str] | None = None, max_retries: int = 1, auto_merge: bool = True) -> DagRunSummary`
  2. Implement orchestration helpers:
     - `_start_or_resume_run(dag_file: str, options: DagRunOptions, session_root: str) -> tuple[int, DagDefinition, dict]`
     - `_reconcile_orphan_panes(dag: DagDefinition, run_id: int, session_root: str) -> None`
     - `_finalize_run(run_id: int, dag: DagDefinition, task_states: dict, session_root: str) -> DagRunSummary`
     - `merge_dag(dag_file: str) -> DagRunSummary`
  3. `_start_or_resume_run`:
     - Check for existing open run (same absolute DAG path)
     - Validate DAG file hash (SHA-256 of raw bytes before parsing)
     - If resuming, call `_reconcile_orphan_panes` before continuing
     - Reconstruct progress from `dag_tasks` rows, not `state_json`
  4. `_reconcile_orphan_panes`:
     - Scan `panes` table for slugs matching DAG task patterns
     - Adopt alive orphans, close dead orphans before re-dispatch
  5. `run_dag` drives tiers 0..N:
     - Apply `--skip` with transitive dependent propagation (close already-dispatched panes for newly-skipped tasks)
     - Apply `--tier` limit
     - Call `_run_single_tier` per tier
     - If `--no-auto-merge`, set run status to `awaiting_merge`
  6. `merge_dag`:
     - Load `awaiting_merge` run for the DAG file
     - Merge `reviewed_pass` tasks in canonical topological order
     - Update run status to `completed` on success
  7. Dry-run renders the execution plan via `render_dry_run()` without creating panes or DB rows.
  8. Add tests for:
     - dry-run output
     - multi-tier merge order
     - `--tier` limiting
     - `--no-auto-merge` leaving panes unmerged + `merge_dag` completing them
     - resume with orphan pane reconciliation
  9. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
     - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Add multi-tier DAG orchestration`

### T3: Escalation + retry logic

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `tests/test_dag.py`
- Depends on: `T2a`
- Spec:
  1. Implement attempt control helpers:
     - `_run_task_until_terminal(...) -> dict`
     - `_retry_same_agent(...) -> dict | None`
     - `_escalate_to_next_agent(...) -> dict | None`
     - `_augment_prompt_with_review(original_prompt: str, review_result: dict | None, pane_slug: str, session_root: str) -> str`
     - `_task_failure_reason(wait_result: dict | Exception | None, review_result: dict | None) -> str`
  2. Do NOT call `maybe_auto_retry` from `retry.py`. The DAG runner implements its own attempt loop. You may reuse `retry_context` from `retry.py` for prompt augmentation.
  3. `_augment_prompt_with_review` format:
     - Prefix: `"The previous attempt failed. Issues found:\n"`
     - If `review_result` has `issues`, format each as a bullet point
     - Append log tail from `retry_context()` (from `retry.py`)
     - Append the original prompt after a separator
  4. Handle these rules explicitly:
     - create/health-check failure -> next agent in escalation chain
     - timeout -> next agent in escalation chain
     - `commit_count == 0` -> next agent in escalation chain
     - `verdict != "safe"` -> retry same agent up to `max_retries`, then escalate
     - pane ends `failed` or `abandoned` -> retry same agent up to `max_retries`, then escalate
  5. Use:
     - `retry_worker_pane(...)`
     - `escalate_worker_pane(...)`
  6. Persist on every transition:
     - current agent
     - current attempt
     - current pane slug
     - last error
  7. Emit escalation events with reason codes:
     - `health_check_failed`
     - `timeout`
     - `zero_commit`
     - `review_failed`
     - `runtime_failed`
  8. Mark the task `failed` only after the whole escalation chain is exhausted.
  9. Add tests for:
     - review fail then retry success
     - review fail then escalate
     - zero-commit immediate escalation
     - timeout escalation
     - health-check failure skipping the first agent
     - exhausted chain causing transitive dependent skip
     - prompt augmentation output format
  10. Run:
      - `uv run pytest tests/test_dag.py -q -m unit`
      - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
      - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Add DAG retry and escalation`

### T4: CLI command (run + merge)

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/cli/dag_cmd.py` (new)
  - `src/dgov/cli/__init__.py`
  - `tests/test_dgov_cli.py`
- Depends on: `T2b`, `T3`
- Spec:
  1. Create a Click group `dag` with subcommands `run` and `merge`.
  2. Implement `dgov dag run`:
     - `dgov dag run <dagfile> --dry-run`
     - `dgov dag run <dagfile> --tier N`
     - `dgov dag run <dagfile> --skip <slug>` repeatable
     - `dgov dag run <dagfile> --max-retries N`
     - `dgov dag run <dagfile> --auto-merge/--no-auto-merge`
  3. Implement `dgov dag merge`:
     - `dgov dag merge <dagfile>`
     - Calls `merge_dag(dag_file)` from `dag.py`
     - Prints JSON summary
     - Returns non-zero on merge conflict
  4. `--tier` is zero-based and inclusive.
  5. Pass the options straight into `run_dag(...)` / `merge_dag(...)`.
  6. For dry-run, print the rendered execution plan.
  7. For non-dry-run, print the JSON summary and return non-zero if `summary.failed` is non-empty or if the run stopped on merge conflict.
  8. Register the new command in `src/dgov/cli/__init__.py`.
  9. Add CLI tests for:
     - argument parsing
     - repeated `--skip`
     - dry-run output path
     - exit code on failed DAG summary
     - `merge` subcommand invocation
  10. Run:
      - `uv run pytest tests/test_dgov_cli.py -q -m unit`
      - `uv run ruff check src/dgov/cli/dag_cmd.py src/dgov/cli/__init__.py tests/test_dgov_cli.py`
      - `uv run ruff format src/dgov/cli/dag_cmd.py src/dgov/cli/__init__.py tests/test_dgov_cli.py`
- Commit: `Add dag run and merge CLI commands`

### T5: DAG state persistence

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/persistence.py`
  - `tests/test_dgov_state.py`
- Depends on: none
- Spec:
  1. Add DDL for:
     - `dag_runs`
     - `dag_tasks`
  2. Add public helpers:
     - `ensure_dag_tables(session_root: str) -> None`
     - `create_dag_run(session_root: str, dag_file: str, started_at: str, status: str, current_tier: int, state_json: dict) -> int`
     - `get_open_dag_run(session_root: str, dag_file: str) -> dict | None`
     - `get_dag_run(session_root: str, dag_run_id: int) -> dict | None`
     - `update_dag_run(...) -> None`
     - `upsert_dag_task(...) -> None`
     - `list_dag_tasks(session_root: str, dag_run_id: int) -> list[dict]`
  3. Store the absolute DAG path and DAG file hash in `state_json` so resume can refuse mismatched specs.
  4. Keep this in the existing `state.db`; do not create a second SQLite file.
  5. Add tests for:
     - table creation
     - run insert/update
     - task upsert
     - resume lookup by absolute DAG path
  6. Run:
     - `uv run pytest tests/test_dgov_state.py -q -m unit`
     - `uv run ruff check src/dgov/persistence.py tests/test_dgov_state.py`
     - `uv run ruff format src/dgov/persistence.py tests/test_dgov_state.py`
- Commit: `Persist DAG run state`

### T6: Event emission + dashboard integration

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/persistence.py`
  - `tests/test_dgov_state.py`
  - `tests/test_dag.py`
- Depends on: `T5`
- Spec:
  1. Extend `VALID_EVENTS` with:
     - `dag_started`
     - `dag_tier_started`
     - `dag_task_dispatched`
     - `dag_task_completed`
     - `dag_task_failed`
     - `dag_task_escalated`
     - `dag_tier_completed`
     - `dag_completed`
     - `dag_failed`
  2. Do not add new dashboard rendering code unless needed. The existing dashboards already consume `read_events()` generically.
  3. Define event payload expectations in tests:
     - run-level events use `pane="dag/<run_id>"`
     - task-level events use `pane=<task_slug>` with `dag_run_id` in payload
  4. Add tests proving `emit_event()` accepts the new names and `read_events()` returns the payload intact.
  5. Run:
     - `uv run pytest tests/test_dgov_state.py tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/persistence.py tests/test_dgov_state.py tests/test_dag.py`
     - `uv run ruff format src/dgov/persistence.py tests/test_dgov_state.py tests/test_dag.py`
- Commit: `Add DAG lifecycle events`

### T7: Convert `DAG-DASHBOARD.md` to the machine-readable format

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `tests/fixtures/dashboard_dag.toml` (new)
  - `tests/test_dag.py`
- Depends on: `T0`
- Spec:
  1. Encode the revised dashboard DAG as TOML, not prose.
  2. Preserve:
     - task slugs
     - tier-driving dependencies
     - file create/edit lists
     - primary agent assignment
     - escalation chain
     - prompts
     - commit messages
  3. Do not invent fields outside the parser schema.
  4. Add a parser test that loads this fixture and verifies:
     - the expected task count
     - expected dependencies
     - expected tier shape
  5. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check tests/test_dag.py`
     - `uv run ruff format tests/test_dag.py`
- Commit: `Add dashboard DAG fixture`

### T8: Integration tests

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `tests/test_dag.py`
  - `tests/test_dgov_cli.py`
- Depends on: `T2a`, `T2b`, `T3`, `T4`, `T5`, `T6`, `T7`
- Spec:
  1. Add runner-level tests using mocks only at system boundaries:
     - `create_worker_pane`
     - `wait_worker_pane`
     - `review_worker_pane`
     - `retry_worker_pane`
     - `escalate_worker_pane`
     - `merge_worker_pane`
     - `close_worker_pane`
  2. Cover:
     - successful two-tier DAG
     - skip propagation through dependents
     - resume from partially populated `dag_runs` and `dag_tasks`
     - zero-commit escalation
     - review-fail retry then escalation
     - health-check failure on initial dispatch
     - merge conflict stopping the DAG after partial success
     - `--no-auto-merge` summary shape
     - fixture parse and tier-match for `dashboard_dag.toml`
  3. Verify at least one unhappy-path test can fail for the right reason before finalizing:
     - temporarily force a wrong expected status or disable the retry branch
     - run the targeted test
     - confirm failure
     - restore the correct assertion and rerun
  4. Run:
     - `uv run pytest tests/test_dag.py tests/test_dgov_cli.py -q -m unit`
     - `uv run ruff check tests/test_dag.py tests/test_dgov_cli.py`
     - `uv run ruff format tests/test_dag.py tests/test_dgov_cli.py`
- Commit: `Test DAG runner end to end`

## Escalation Policy Summary

1. Use `hunter` first everywhere.
2. Escalate to `gemini` on parser ambiguity, state-machine bugs, or retry-loop dead ends.
3. Escalate to `claude` only if `gemini` also stalls or produces a spec/code mismatch.

## Definition of Done

1. `dgov dag run <dagfile>` executes a TOML DAG through create, wait, review, retry/escalate, merge, and summary.
2. `dgov dag merge <dagfile>` merges `awaiting_merge` runs in topological order.
3. Resume after governor death reconciles orphan panes before re-dispatching.
4. All DAG state required for resume lives in `state.db`. `dag_tasks.status` is the source of truth.
5. DAG events appear in the existing event feed.
6. The dashboard DAG fixture parses and tiers correctly.
7. Targeted unit tests pass. No full-suite run.
