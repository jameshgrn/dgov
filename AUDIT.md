# dgov Design Efficiency Audit

Last updated: 2026-03-15. 21 of 30 findings resolved. Remaining items tracked below.

Scope: `src/dgov/` only. This is a static design audit; I did not modify source files or run the full test suite.

Method: repo-wide symbol/reference search plus manual call-graph tracing. In the dead-code section, "dead" means no production reference from `src/dgov` (test-only references do not count).

## 1. Dead code

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| **FIXED** ~~`src/dgov/agents.py:514`~~ | low | ~~`build_resume_command()` has no production caller.~~ | Deleted. |
| **FIXED** ~~`src/dgov/models.py:8`~~ | low | ~~`TaskSpec` is runtime-dead.~~ | Deleted. |
| **FIXED** ~~`src/dgov/models.py:27`~~ | low | ~~`ConflictDetails` is runtime-dead.~~ | Deleted. |
| **FIXED** ~~`src/dgov/tmux.py:23`, `src/dgov/tmux.py:33`, `src/dgov/tmux.py:81`, `src/dgov/tmux.py:146`, `src/dgov/tmux.py:252`~~ | low | ~~`has_session()`, `ensure_session()`, `update_pane_status()`, `list_panes()`, and `select_pane()` are production-dead.~~ | Removed. |

## 2. Redundant logic

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/panes.py:254`, `src/dgov/panes.py:1063` | medium | `create_worker_pane()` and `resume_worker_pane()` duplicate the same launch pipeline: health checks, concurrency checks, tmux setup, logging, hook invocation, protected-file warning, done-signal creation, and agent launch. | Extract one shared launch helper that takes "new worktree" vs "existing worktree" as the only branch point. |
| `src/dgov/preflight.py:322`, `src/dgov/preflight.py:361`, `src/dgov/panes.py:313`, `src/dgov/panes.py:1103` | medium | Agent health checks and concurrency guards are implemented twice: once in preflight and again in pane creation/resume. | Move health/concurrency validation into reusable helpers called by both preflight and pane launch paths. |
| `src/dgov/batch.py:214`, `src/dgov/review_fix.py:231`, `src/dgov/review_fix.py:328` | medium | Batch and review-fix reimplement polling loops instead of reusing `waiter.wait_worker_pane()` / `wait_all_worker_panes()`. | Reuse waiter APIs and let them be the single place that defines "done". |
| **FIXED** ~~`src/dgov/blame.py:118`, `src/dgov/retry.py:24`, `src/dgov/panes.py:150`~~ | low | ~~Event-journal parsing duplicated in three modules.~~ | Centralized in `persistence.py` via `read_events()`. |
| `src/dgov/review_fix.py:245`, `src/dgov/review_fix.py:251` | low | Review output is parsed twice for every worker: once to collect findings and again to emit finding events. | Parse once, reuse the parsed list. |
| **FIXED** ~~`src/dgov/cli.py:652`, `src/dgov/dashboard.py:50`~~ | low | ~~Duration formatting duplicated in CLI and dashboard.~~ | Unified. |

## 3. Over-engineering

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/backend.py:15` | medium | `WorkerBackend`/`TmuxBackend` add an abstraction layer for alternate runtimes that do not exist. Production only ever uses tmux. | Collapse to a concrete tmux service until a second backend actually exists. |
| **FIXED** ~~`src/dgov/state.py:9`~~ | low | ~~`state.py` is a one-function wrapper over `list_worker_panes()` plus two counts.~~ | Deleted; `get_status()` inlined into CLI. |
| `src/dgov/strategy.py:14` | medium | Task routing uses an LLM call for a binary decision. | Use a cheap local heuristic first; only call the model when the prompt is ambiguous. |
| `src/dgov/merger.py:648` | low | `merge_worker_pane_with_close()` is a wrapper around `merge_worker_pane()` that usually does nothing extra. | Delete the wrapper and let CLI choose plain `merge_worker_pane()`. |

## 4. Module boundaries

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/panes.py:254`, `src/dgov/panes.py:573`, `src/dgov/panes.py:689`, `src/dgov/panes.py:842`, `src/dgov/panes.py:935`, `src/dgov/panes.py:1055` | high | `panes.py` is a monolith: worktree lifecycle, live status, review, diff, rebase, escalation, retry, and resume all live in one 1,255-line module. | Split it into at least `lifecycle`, `status`, `inspection`, and `recovery` modules. |
| `src/dgov/panes.py:35`, `src/dgov/waiter.py:105`, `src/dgov/merger.py:493` | medium | The dependency graph is only "clean" because cycles are hidden behind local imports. | Break the cycle with a small state/worker service layer. |
| `src/dgov/panes.py:19`, `src/dgov/batch.py:11`, `src/dgov/retry.py:12`, `src/dgov/review_fix.py:12` | medium | "Private" persistence helpers are imported all over the codebase. | Promote needed operations to a real public persistence API. |
| `src/dgov/dashboard.py:184`, `src/dgov/panes.py:785` | medium | The dashboard expects a `diff_stat` field, but `review_worker_pane()` exposes `stat`. | Define a typed return shape or a single renderer-facing DTO. |

## 5. Performance

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| **FIXED** ~~`src/dgov/panes.py:172`, `src/dgov/panes.py:573`~~ | high | ~~`list_worker_panes()` computes freshness per pane unconditionally, triggering ~150 git calls at 50 panes.~~ | Freshness is now opt-in via `include_freshness=False`. |
| **FIXED** ~~`src/dgov/dashboard.py:130`, `src/dgov/dashboard.py:212`~~ | high | ~~The dashboard refresh thread calls `list_worker_panes()` every 2 seconds, multiplying subprocess load.~~ | Dashboard now passes `include_freshness=False`, decoupling from subprocess-heavy freshness computation. |
| **FIXED** ~~`src/dgov/preflight.py:163`, `src/dgov/preflight.py:441`, `src/dgov/cli.py:362`~~ | high | ~~`pane create` runs `check_deps()` by default, executing `uv sync --locked` in the hot path.~~ | `check_deps()` now skipped by default; opt-in via `skip_deps=False`. |
| `src/dgov/blame.py:181`, `src/dgov/blame.py:306` | medium | `blame_lines()` can call `_slug_from_sha_subject()` once per line, creating O(n) subprocesses. | Memoize SHA resolution for the blame run. |
| `src/dgov/preflight.py:245`, `src/dgov/preflight.py:279` | medium | `check_stale_worktrees()` and `check_file_locks()` call full `list_worker_panes()` when they only need stored metadata. | Use a cheap persistence-level query instead. |
| `src/dgov/review_fix.py:351` | medium | Review-fix runs full test suite after every merged fix pane. | Run only targeted tests derived from affected files. |
| **FIXED** ~~`src/dgov/persistence.py:184`~~ | medium | ~~Every persistence operation opens a new SQLite connection, reasserts WAL mode, reruns `CREATE TABLE`.~~ | Connection cache per (db_path, thread) with busy_timeout=5000. |
| **FIXED** ~~`src/dgov/panes.py:777`, `src/dgov/panes.py:780`, `src/dgov/retry.py:59`, `src/dgov/panes.py:150`~~ | low | ~~`review_worker_pane()` triggers two full event-log scans on every review call.~~ | Events read once at panes.py:828; both retry_count and auto_respond_count derived from single pass. |

## 6. API surface

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| `src/dgov/cli.py:181`, `src/dgov/cli.py:194`, `src/dgov/cli.py:204`, `src/dgov/cli.py:214`, `src/dgov/cli.py:224`, `src/dgov/cli.py:234` | low | The CLI exposes six public commands for one utility-pane concept. Five are thin hard-coded presets over `pane util`. | Keep `pane util` plus one or two truly common shortcuts; remove the rest. |
| **FIXED** ~~`src/dgov/cli.py:903`, `src/dgov/cli.py:919`~~ | low | ~~`pane interact` and `pane respond` are the same command with two names.~~ | Alias removed. |
| `src/dgov/panes.py:19`, `src/dgov/waiter.py:105`, `src/dgov/merger.py:493` | medium | Sibling modules call underscore-prefixed helpers directly. | Publish a narrow orchestration/persistence API. |
| **FIXED** ~~`src/dgov/dashboard.py:184`, `src/dgov/panes.py:785`~~ | medium | ~~The broken `diff_stat`/`stat` contract between dashboard and panes.~~ | Contract aligned. |

## 7. Error handling

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| **FIXED** ~~`src/dgov/batch.py:208`~~ | medium | ~~Batch pane creation collapses all launch failures into `"unknown error"`.~~ | Real exception message plus task id and operation context now returned. |
| `src/dgov/dashboard.py:190`, `src/dgov/dashboard.py:204`, `src/dgov/dashboard.py:637`, `src/dgov/dashboard.py:644` | medium | The dashboard swallows exceptions in detail rendering and action execution. | Surface exception text in UI state and keep an error banner. |
| **FIXED** ~~`src/dgov/persistence.py:204`, `src/dgov/retry.py:34`, `src/dgov/blame.py:127`~~ | medium | ~~Corrupt JSON in pane metadata or event journal silently ignored.~~ | Now logged with context. |
| **FIXED** ~~`src/dgov/agents.py:344`, `src/dgov/agents.py:357`, `src/dgov/openrouter.py:44`~~ | low | ~~Invalid TOML config silently swallowed.~~ | Now logs warning with offending path. |
| **FIXED** ~~`src/dgov/preflight.py:485`~~ | medium | ~~`_fix_agent_health()` runs the first health-fix for any agent, not the one that failed.~~ | Now threads the specific agent_id. |

## 8. State management

| File:line | Severity | Issue | Suggested fix |
|---|---|---|---|
| **FIXED** ~~`src/dgov/persistence.py:184`~~ | high | ~~SQLite opened with default settings only — no `busy_timeout`, no retry, no write coordination beyond WAL.~~ | Connection cache with busy_timeout=5000 and WAL mode. |
| **FIXED** ~~`src/dgov/persistence.py:272`~~ | medium | ~~`_update_pane_state()` does read-check-write without transactional guard.~~ | Uses UPDATE with expected current state in WHERE clause. |
| `src/dgov/persistence.py:47`, `src/dgov/panes.py:454`, `src/dgov/panes.py:456`, `src/dgov/panes.py:562`, `src/dgov/panes.py:1041` | medium | State DB writes and event-journal writes are separate operations with no transaction tying them together. | Move events into SQLite or wrap state transition + event append in one durable unit. |
| `src/dgov/persistence.py:61`, `src/dgov/retry.py:24`, `src/dgov/blame.py:118` | medium | The JSONL event log has no locking or validation strategy, and readers silently skip malformed lines. | Add append locking or move events into SQLite. |
