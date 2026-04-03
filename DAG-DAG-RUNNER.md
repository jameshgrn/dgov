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
  └─ T2: Execution engine core                      (depends on T1, T5, T6)

TIER 3
  └─ T3: Escalation + retry logic                   (depends on T2)

TIER 4
  └─ T4: CLI command                                (depends on T2, T3)

TIER 5
  └─ T8: Integration tests                          (depends on T2, T3, T4, T5, T6, T7)
```

## Merge Order

```text
T0 -> T5 -> T1 -> T6 -> T7 -> T2 -> T3 -> T4 -> T8
```

Reasoning:

1. `T0`, `T1`, `T2`, and `T3` all touch `src/dgov/dag.py`, so they must merge in strict order.
2. `T5` and `T6` both touch `src/dgov/persistence.py`, so they must merge in strict order.
3. `T8` comes last because it needs the real runner surface, the CLI, and the fixture.

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

### T2: Execution engine core

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `tests/test_dag.py`
- Depends on: `T1`, `T5`, `T6`
- Spec:
  1. Implement the runner entry point:
     - `run_dag(dag_file: str, *, dry_run: bool = False, tier_limit: int | None = None, skip: set[str] | None = None, max_retries: int = 1, auto_merge: bool = True) -> DagRunSummary`
  2. Implement execution helpers:
     - `_start_or_resume_run(...) -> tuple[int, DagDefinition, DagRunOptions, dict]`
     - `_dispatch_task(...) -> dict`
     - `_wait_for_tier(...) -> dict[str, dict]`
     - `_review_passed_task(...) -> dict`
     - `_merge_tasks_in_order(...) -> list[str]`
     - `_finalize_run(...) -> DagRunSummary`
  3. Use the governor-side Python APIs directly:
     - `create_worker_pane`
     - `wait_worker_pane(..., auto_retry=False)`
     - `review_worker_pane`
     - `merge_worker_pane`
  4. For each tier:
     - dispatch all ready tasks
     - wait for all active panes in the tier
     - review every finished pane
     - merge reviewed-pass tasks in canonical topological order if `auto_merge=True`
  5. Update pane state explicitly with `update_pane_state()`:
     - `reviewed_pass`
     - `reviewed_fail`
  6. On successful merge, do not call `close_worker_pane()` again. `merge_worker_pane()` already removes the pane and worktree on success.
  7. If `merge_worker_pane()` returns an error or conflicts, stop the DAG immediately and return a partial summary.
  8. Add tests for:
     - dry-run output
     - successful single-tier run
     - multi-tier merge order
     - `--tier` limiting
     - `--no-auto-merge` leaving reviewed-pass panes unmerged
  9. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
     - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Add DAG execution loop`

### T3: Escalation + retry logic

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/dag.py`
  - `tests/test_dag.py`
- Depends on: `T2`
- Spec:
  1. Implement attempt control helpers:
     - `_run_task_until_terminal(...) -> dict`
     - `_retry_same_agent(...) -> dict | None`
     - `_escalate_to_next_agent(...) -> dict | None`
     - `_augment_prompt_with_review(...) -> str`
     - `_task_failure_reason(wait_result: dict | Exception | None, review_result: dict | None) -> str`
  2. Handle these rules explicitly:
     - create/health-check failure -> next agent in escalation chain
     - timeout -> next agent in escalation chain
     - `commit_count == 0` -> next agent in escalation chain
     - `verdict != "safe"` -> retry same agent up to `max_retries`, then escalate
     - pane ends `failed` or `abandoned` -> retry same agent up to `max_retries`, then escalate
  3. Use:
     - `retry_worker_pane(...)`
     - `escalate_worker_pane(...)`
  4. Persist on every transition:
     - current agent
     - current attempt
     - current pane slug
     - last error
  5. Emit escalation events with reason codes:
     - `health_check_failed`
     - `timeout`
     - `zero_commit`
     - `review_failed`
     - `runtime_failed`
  6. Mark the task `failed` only after the whole escalation chain is exhausted.
  7. Add tests for:
     - review fail then retry success
     - review fail then escalate
     - zero-commit immediate escalation
     - timeout escalation
     - health-check failure skipping the first agent
     - exhausted chain causing transitive dependent skip
  8. Run:
     - `uv run pytest tests/test_dag.py -q -m unit`
     - `uv run ruff check src/dgov/dag.py tests/test_dag.py`
     - `uv run ruff format src/dgov/dag.py tests/test_dag.py`
- Commit: `Add DAG retry and escalation`

### T4: CLI command

- Agent: `hunter`
- Escalation: `gemini`, `claude`
- Files:
  - `src/dgov/cli/dag_cmd.py` (new)
  - `src/dgov/cli/__init__.py`
  - `tests/test_dgov_cli.py`
- Depends on: `T2`, `T3`
- Spec:
  1. Create a Click group `dag` with subcommand `run`.
  2. Implement:
     - `dgov dag run <dagfile> --dry-run`
     - `dgov dag run <dagfile> --tier N`
     - `dgov dag run <dagfile> --skip <slug>` repeatable
     - `dgov dag run <dagfile> --max-retries N`
     - `dgov dag run <dagfile> --auto-merge/--no-auto-merge`
  3. `--tier` is zero-based and inclusive.
  4. Pass the options straight into `run_dag(...)`.
  5. For dry-run, print the rendered execution plan.
  6. For non-dry-run, print the JSON summary and return non-zero if `summary.failed` is non-empty or if the run stopped on merge conflict.
  7. Register the new command in `src/dgov/cli/__init__.py`.
  8. Add CLI tests for:
     - argument parsing
     - repeated `--skip`
     - dry-run output path
     - exit code on failed DAG summary
  9. Run:
     - `uv run pytest tests/test_dgov_cli.py -q -m unit`
     - `uv run ruff check src/dgov/cli/dag_cmd.py src/dgov/cli/__init__.py tests/test_dgov_cli.py`
     - `uv run ruff format src/dgov/cli/dag_cmd.py src/dgov/cli/__init__.py tests/test_dgov_cli.py`
- Commit: `Add dag run CLI command`

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
- Depends on: `T0`, `T1`
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
- Depends on: `T2`, `T3`, `T4`, `T5`, `T6`, `T7`
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
2. All DAG state required for resume lives in `state.db`.
3. DAG events appear in the existing event feed.
4. The dashboard DAG fixture parses and tiers correctly.
5. Targeted unit tests pass. No full-suite run.
