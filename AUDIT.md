# dgov Design Efficiency Audit

Scope: `src/dgov/` only. This is a static design audit; I did not modify source files or run the full test suite.

Method: repo-wide symbol/reference search plus manual call-graph tracing. In the dead-code section, "dead" means no production reference from `src/dgov` (test-only references do not count).

## 1. Dead code

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/agents.py:514` | low | `build_resume_command()` has no production caller. `resume_worker_pane()` manually rebuilds resume behavior instead of using it, so the dedicated abstraction is dead. | Delete `build_resume_command()`, or make `resume_worker_pane()` use it and remove the duplicated launch path. |
| `src/dgov/models.py:8` | low | `TaskSpec` is runtime-dead. Repo-wide references are test-only. | Delete it until batch/task specs actually need a typed runtime model. |
| `src/dgov/models.py:27` | low | `ConflictDetails` is runtime-dead. `MergeResult.conflicts` exists, but no runtime path constructs `ConflictDetails`. | Delete `ConflictDetails`, or wire real conflict objects through merge paths. |
| `src/dgov/tmux.py:23`, `src/dgov/tmux.py:33`, `src/dgov/tmux.py:81`, `src/dgov/tmux.py:146`, `src/dgov/tmux.py:252` | low | `has_session()`, `ensure_session()`, `update_pane_status()`, `list_panes()`, and `select_pane()` are production-dead. They are only referenced by tests. | Remove them, or reintroduce them only when a production caller appears. |

## 2. Redundant logic

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/panes.py:254`, `src/dgov/panes.py:1063` | medium | `create_worker_pane()` and `resume_worker_pane()` duplicate the same launch pipeline: health checks, concurrency checks, tmux setup, logging, hook invocation, protected-file warning, done-signal creation, and agent launch. This is already drifting from the unused `build_resume_command()` abstraction. | Extract one shared launch helper that takes "new worktree" vs "existing worktree" as the only branch point. |
| `src/dgov/preflight.py:322`, `src/dgov/preflight.py:361`, `src/dgov/panes.py:313`, `src/dgov/panes.py:1103` | medium | Agent health checks and concurrency guards are implemented twice: once in preflight and again in pane creation/resume. That creates multiple sources of truth for launch policy. | Move health/concurrency validation into reusable helpers called by both preflight and pane launch paths. |
| `src/dgov/batch.py:214`, `src/dgov/review_fix.py:231`, `src/dgov/review_fix.py:328` | medium | Batch and review-fix reimplement polling loops instead of reusing `waiter.wait_worker_pane()` / `wait_all_worker_panes()`. Those loops skip blocked detection, auto-retry, and timeout behavior that the waiter already owns. | Reuse waiter APIs and let them be the single place that defines "done". |
| `src/dgov/blame.py:118`, `src/dgov/retry.py:24`, `src/dgov/panes.py:150` | low | Event-journal parsing is duplicated in three modules, each with its own silent JSON-corruption behavior. | Centralize event-log read helpers in `persistence.py`. |
| `src/dgov/review_fix.py:245`, `src/dgov/review_fix.py:251` | low | Review output is parsed twice for every worker: once to collect findings and again to emit finding events. | Parse once, reuse the parsed list. |
| `src/dgov/cli.py:652`, `src/dgov/dashboard.py:50` | low | Duration formatting is duplicated in CLI and dashboard with slightly different behavior. | Keep one formatter in a shared helper, or inline one of them and delete the other. |

## 3. Over-engineering

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/backend.py:15` | medium | `WorkerBackend`/`TmuxBackend` add an abstraction layer for alternate runtimes that do not exist in this repo. Production only ever uses tmux; `set_backend()` is test-only. The result is pervasive indirection for hypothetical backends. | Collapse to a concrete tmux service until a second backend actually exists. Reintroduce the protocol when there is a real second implementation. |
| `src/dgov/state.py:9` | low | `state.py` is a one-function wrapper over `list_worker_panes()` plus two counts. It is too small to justify a separate module boundary. | Inline `get_status()` into the CLI or merge `state.py` into `panes.py`. |
| `src/dgov/strategy.py:14` | medium | Task routing uses an LLM call for a binary/mechanical-vs-analytical decision. For a governor tool this is a heavyweight dependency for a mostly heuristic choice. | Use a cheap local heuristic first; only call the model when the prompt is ambiguous. |
| `src/dgov/merger.py:648` | low | `merge_worker_pane_with_close()` is a wrapper around `merge_worker_pane()` even though `merge_worker_pane()` already calls `_full_cleanup()` on success. The wrapper exists mainly to call a close path that usually does nothing. | Delete the wrapper and let CLI choose plain `merge_worker_pane()`. |

## 4. Module boundaries

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/panes.py:254`, `src/dgov/panes.py:573`, `src/dgov/panes.py:689`, `src/dgov/panes.py:842`, `src/dgov/panes.py:935`, `src/dgov/panes.py:1055` | high | `panes.py` is a monolith: worktree lifecycle, live status, review, diff, rebase, escalation, retry, and resume all live in one 1,255-line module. That makes the dependency graph center on one god-module. | Split it into at least `lifecycle`, `status`, `inspection`, and `recovery` modules. |
| `src/dgov/panes.py:35`, `src/dgov/waiter.py:105`, `src/dgov/merger.py:493` | medium | The dependency graph is only "clean" because cycles are hidden behind local imports. `panes` imports waiter at module scope, waiter imports `dgov.panes` inside core functions, and merger also reaches back into `dgov.panes`. | Break the cycle with a small state/worker service layer that waiter and merger can consume without importing pane orchestration. |
| `src/dgov/panes.py:19`, `src/dgov/batch.py:11`, `src/dgov/retry.py:12`, `src/dgov/review_fix.py:12` | medium | "Private" persistence helpers are imported all over the codebase. Underscore names are being used as the real cross-module API, which means the public/private boundary is fake. | Either promote the needed operations to a real public persistence API, or stop reaching into internals from sibling modules. |
| `src/dgov/dashboard.py:184`, `src/dgov/panes.py:785` | medium | The dashboard expects a `diff_stat` field, but `review_worker_pane()` exposes `stat`. That interface mismatch is a module-boundary failure: the contract is implicit and already broken. | Define a typed return shape or a single renderer-facing DTO for pane review data. |

## 5. Performance

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/panes.py:172`, `src/dgov/panes.py:573` | high | `list_worker_panes()` computes freshness per pane, and `_compute_freshness()` can run three git subprocesses per pane. At 50 panes, a single refresh can trigger ~150 git calls before any UI work. | Cache freshness, batch git queries, or make freshness opt-in instead of mandatory on every list operation. |
| `src/dgov/dashboard.py:130`, `src/dgov/dashboard.py:212` | high | The dashboard refresh thread calls `list_worker_panes()` every 2 seconds by default, multiplying the subprocess storm above into a continuous background load. | Decouple dashboard refresh from freshness computation, or only recompute expensive freshness on manual refresh / selected pane. |
| `src/dgov/preflight.py:163`, `src/dgov/preflight.py:441`, `src/dgov/cli.py:362` | high | `pane create` runs `check_deps()` by default, and `check_deps()` executes `uv sync --locked`. That is a heavyweight, side-effectful environment sync in the hot path of worker creation. | Replace it with a cheap read-only health check, or move dependency sync to an explicit command. |
| `src/dgov/blame.py:181`, `src/dgov/blame.py:306` | medium | `blame_lines()` can call `_slug_from_sha_subject()` once per line when SHA-to-slug lookup misses, and `_slug_from_sha_subject()` runs `git log`. That creates O(number of unattributed lines) subprocesses. | Memoize SHA resolution for the blame run, or precompute merge ancestry once per file. |
| `src/dgov/preflight.py:245`, `src/dgov/preflight.py:279` | medium | `check_stale_worktrees()` and `check_file_locks()` call `list_worker_panes()`, which pulls full liveness/freshness status when these checks only need stored pane metadata. | Use `_all_panes()` or a cheap persistence-level query instead of the enriched listing path. |
| `src/dgov/review_fix.py:351` | medium | Review-fix runs `uv run pytest -q --tb=short -x` after every merged fix pane. That is effectively the full suite in a loop, which does not scale and will dominate runtime on any real repo. | Run only targeted tests derived from affected files, or defer one targeted validation pass to the end. |
| `src/dgov/persistence.py:184` | medium | Every persistence operation opens a new SQLite connection, reasserts WAL mode, reruns `CREATE TABLE`, and closes again. Under frequent polling this is avoidable overhead. | Use a small connection factory with initialization-once semantics and a busy timeout. |
| `src/dgov/panes.py:777`, `src/dgov/panes.py:780`, `src/dgov/retry.py:59`, `src/dgov/panes.py:150` | low | `review_worker_pane()` triggers two full event-log scans (`_count_retries()` and `_count_auto_responses()`) on every review call. | Read the journal once per review and derive both counters from one pass. |

## 6. API surface

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/cli.py:181`, `src/dgov/cli.py:194`, `src/dgov/cli.py:204`, `src/dgov/cli.py:214`, `src/dgov/cli.py:224`, `src/dgov/cli.py:234` | low | The CLI exposes six public commands for one utility-pane concept. Five of them are thin hard-coded presets over `pane util`. That is interface sprawl, not capability. | Keep `pane util` plus maybe one or two truly common shortcuts; remove the rest. |
| `src/dgov/cli.py:903`, `src/dgov/cli.py:919` | low | `pane interact` and `pane respond` are the same command with two names and identical behavior. | Pick one name and remove the alias. |
| `src/dgov/panes.py:19`, `src/dgov/waiter.py:105`, `src/dgov/merger.py:493` | medium | The effective package API is much larger than it looks because sibling modules call underscore-prefixed helpers directly. Consumers have to understand internals instead of a stable interface. | Publish a narrow orchestration/persistence API and stop using underscore helpers cross-module. |
| `src/dgov/dashboard.py:184`, `src/dgov/panes.py:785` | medium | The broken `diff_stat`/`stat` contract shows the public data shape between modules is informal and brittle. | Define explicit return types or dataclasses for cross-module results instead of ad hoc dicts. |

## 7. Error handling

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/batch.py:208` | medium | Batch pane creation collapses all launch failures into `"unknown error"`, throwing away the actual exception context. | Return the real exception message plus task id and operation context. |
| `src/dgov/dashboard.py:190`, `src/dgov/dashboard.py:204`, `src/dgov/dashboard.py:637`, `src/dgov/dashboard.py:644` | medium | The dashboard swallows exceptions in detail rendering and action execution. Failures become silent no-ops or generic "unavailable" text, which makes operator diagnosis hard. | Surface the exception text in the UI state and keep an error banner instead of discarding it. |
| `src/dgov/persistence.py:204`, `src/dgov/retry.py:34`, `src/dgov/blame.py:127` | medium | Corrupt JSON in pane metadata or the event journal is silently ignored. That trades visible failure for silent state loss. | Fail fast with context, or at minimum log the corrupt record path and line so the operator knows state was dropped. |
| `src/dgov/agents.py:344`, `src/dgov/agents.py:357`, `src/dgov/openrouter.py:44` | low | Invalid TOML config is swallowed and replaced with `{}`. The user just gets fallback behavior, not a clear configuration error. | Return a structured error or log a warning with the offending path. |
| `src/dgov/preflight.py:485` | medium | `_fix_agent_health()` runs the first health-fix command that succeeds for any agent, not the agent that actually failed preflight. That can apply the wrong fix and still report success. | Thread the failing agent id into the fixer and run only that agent's `health_fix`. |

## 8. State management

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/persistence.py:184` | high | SQLite is opened with default settings only. There is no `busy_timeout`, no retry policy, and no write coordination beyond WAL. With multiple worker processes updating pane state, transient `database is locked` errors are likely. | Set `busy_timeout`, handle retryable lock errors, and centralize DB initialization instead of reopening raw connections everywhere. |
| `src/dgov/persistence.py:272` | medium | `_update_pane_state()` does read-check-write logic without an explicit transactional guard against concurrent updates. Another writer can change the row between validation and update. | Use an explicit transaction and update with the expected current state in the `WHERE` clause. |
| `src/dgov/persistence.py:47`, `src/dgov/panes.py:454`, `src/dgov/panes.py:456`, `src/dgov/panes.py:562`, `src/dgov/panes.py:1041` | medium | State DB writes and event-journal writes are separate operations with no transaction tying them together. Crashes or concurrent failures can leave pane state and history out of sync. | Either move events into SQLite or wrap state transition + event append in one durable unit. |
| `src/dgov/persistence.py:61`, `src/dgov/retry.py:24`, `src/dgov/blame.py:118` | medium | The JSONL event log has no locking or validation strategy, and all readers are written to silently skip malformed lines. That makes event loss invisible and undermines retry/blame correctness under concurrency. | Add append locking or move events into SQLite; do not silently discard malformed records. |

