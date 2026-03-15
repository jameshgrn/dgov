# dgov v0.9.0 roadmap audit

Scope reviewed:

- All Python source under `src/dgov/` (27 files, 9,475 LOC)
- `README.md`
- `pyproject.toml`
- Test suite shape under `tests/`

## 1. Technical debt inventory

### API surface problems

- Private helpers are already part of the de facto public API. [`src/dgov/panes.py:1`](src/dgov/panes.py), [`src/dgov/panes.py:7`](src/dgov/panes.py), [`src/dgov/panes.py:13`](src/dgov/panes.py), [`src/dgov/panes.py:26`](src/dgov/panes.py), [`src/dgov/panes.py:33`](src/dgov/panes.py)
  `dgov.panes` re-exports `_remove_worktree`, `_build_pane_title`, `_create_worktree`, `_full_cleanup`, `_trigger_hook`, `_compute_freshness`, `_count_active_agent_workers`, `_is_done`, and `_wrap_done_signal` for “backward compatibility”. Either those are public and need stable names/types, or they should stop leaking.

- The library layer accepts unknown agents and can create inert panes instead of failing fast. [`src/dgov/lifecycle.py:172`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:293`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:524`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:628`](src/dgov/lifecycle.py)
  `create_worker_pane()` and `resume_worker_pane()` only launch when `agent_def` exists. If a caller bypasses the CLI validation, dgov will still create tmux panes, write state, and return success without actually launching an agent.

- Public function signatures are inconsistent about `project_root` versus `session_root`. [`src/dgov/lifecycle.py:136`](src/dgov/lifecycle.py), [`src/dgov/inspection.py:18`](src/dgov/inspection.py), [`src/dgov/recovery.py:18`](src/dgov/recovery.py), [`src/dgov/waiter.py:298`](src/dgov/waiter.py), [`src/dgov/waiter.py:460`](src/dgov/waiter.py), [`src/dgov/persistence.py:319`](src/dgov/persistence.py)
  Some public APIs take both roots, some only `session_root`, some only `project_root`, and some infer one from the other. That inconsistency leaks into CLI plumbing and makes alternative session layouts fragile.

- Public APIs still trade in anonymous dicts instead of typed result objects. [`src/dgov/inspection.py:18`](src/dgov/inspection.py), [`src/dgov/merger.py:547`](src/dgov/merger.py), [`src/dgov/recovery.py:18`](src/dgov/recovery.py), [`src/dgov/waiter.py:479`](src/dgov/waiter.py), [`src/dgov/batch.py:283`](src/dgov/batch.py), [`src/dgov/openrouter.py:227`](src/dgov/openrouter.py)
  There is one typed result model (`MergeResult`), but most of the orchestration surface returns raw dicts with ad hoc keys like `error`, `hint`, `merged`, `retried`, `warning`, `phase`, or `method`. This is brittle and hard to evolve without breakage.

- `cli.py` still contains library-grade orchestration logic. [`src/dgov/cli.py:91`](src/dgov/cli.py), [`src/dgov/cli.py:104`](src/dgov/cli.py), [`src/dgov/cli.py:245`](src/dgov/cli.py), [`src/dgov/cli.py:316`](src/dgov/cli.py), [`src/dgov/cli.py:554`](src/dgov/cli.py), [`src/dgov/cli.py:1416`](src/dgov/cli.py), [`src/dgov/cli.py:1470`](src/dgov/cli.py)
  Governor bootstrapping, template rendering, task classification, preflight/fix orchestration, `merge-all`, init scaffolding, and doctor diagnostics all live inside Click handlers instead of callable service functions.

- `write_project_config()` rewrites TOML structurally instead of updating it semantically. [`src/dgov/agents.py:383`](src/dgov/agents.py), [`src/dgov/agents.py:400`](src/dgov/agents.py)
  It discards comments, nested structure, non-string scalar types, and ordering. That makes config persistence lossy and unsafe as the config surface grows.

### Structural debt

- `create_worker_pane()` and `resume_worker_pane()` duplicate the same launch pipeline. [`src/dgov/lifecycle.py:223`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:242`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:288`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:567`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:583`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:621`](src/dgov/lifecycle.py)
  Pane creation, title locking, layout, auth-env scrubbing, logging, env injection, hook dispatch, prompt rewrite, done-signal setup, and agent launch are implemented twice with small variations. The duplication is already visible in bug drift: resume clears the old done signal, create does not; create accepts `env_vars`, resume ignores caller-provided env overrides.

- There is a real import-cycle knot around the pane orchestration surface. [`src/dgov/panes.py:7`](src/dgov/panes.py), [`src/dgov/lifecycle.py:30`](src/dgov/lifecycle.py), [`src/dgov/status.py:19`](src/dgov/status.py), [`src/dgov/waiter.py:105`](src/dgov/waiter.py), [`src/dgov/recovery.py:8`](src/dgov/recovery.py), [`src/dgov/retry.py:140`](src/dgov/retry.py), [`src/dgov/inspection.py:15`](src/dgov/inspection.py)
  The effective SCC is `panes -> lifecycle/status/inspection/recovery -> waiter/retry -> panes`. Most of it is hidden behind local imports, which avoids import-time crashes but not design coupling.

- `persistence.py` is doing four jobs: DB connection pooling, event logging, state machine validation, and UI title mutation. [`src/dgov/persistence.py:18`](src/dgov/persistence.py), [`src/dgov/persistence.py:53`](src/dgov/persistence.py), [`src/dgov/persistence.py:93`](src/dgov/persistence.py), [`src/dgov/persistence.py:401`](src/dgov/persistence.py)
  The store layer should not know about tmux pane titles.

- `merger.py` is a god module. [`src/dgov/merger.py:20`](src/dgov/merger.py), [`src/dgov/merger.py:211`](src/dgov/merger.py), [`src/dgov/merger.py:277`](src/dgov/merger.py), [`src/dgov/merger.py:315`](src/dgov/merger.py), [`src/dgov/merger.py:442`](src/dgov/merger.py), [`src/dgov/merger.py:547`](src/dgov/merger.py)
  It owns merge plumbing, dirty-worktree handling, conflict prediction, agent-driven conflict resolution, auto-commit, protected-file restoration, post-merge linting, state transitions, events, and cleanup.

- `cli.py` is still a monolith at 1,615 lines. [`src/dgov/cli.py:1`](src/dgov/cli.py)
  Command grouping exists at the Click level, but the file is still one module with heterogeneous concerns and no internal service boundary.

- The configuration/security model is internally contradictory. [`src/dgov/agents.py:306`](src/dgov/agents.py), [`src/dgov/agents.py:311`](src/dgov/agents.py), [`src/dgov/agents.py:319`](src/dgov/agents.py)
  The comment says project-local agent config “cannot define shell commands”, but the implementation only strips `health_check` and `health_fix`. Project config can still define new agents or override `command`, `no_prompt_command`, `default_flags`, and `env`.

### Reliability gaps

- Event writes are not retried on SQLite lock contention. [`src/dgov/persistence.py:53`](src/dgov/persistence.py), [`src/dgov/persistence.py:267`](src/dgov/persistence.py)
  Pane mutations go through `_retry_on_lock()`. `emit_event()` does not. Under concurrent writers, event loss or `database is locked` is still possible even though the pane table paths were hardened.

- Concurrency testing covers `add_pane()` only, not event writes or state/event interleaving. [`tests/test_concurrent_workers.py:164`](tests/test_concurrent_workers.py)
  There is no comparable stress test for `emit_event()`, `update_pane_state()`, or mixed read/write workloads.

- Cleanup updates pane state before resource cleanup, so state can say “closed” or “merged” while tmux/worktree cleanup failed. [`src/dgov/lifecycle.py:459`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:461`](src/dgov/lifecycle.py), [`src/dgov/merger.py:624`](src/dgov/merger.py), [`src/dgov/merger.py:641`](src/dgov/merger.py)
  The state machine currently records outcomes before destructive teardown succeeds.

- `_full_cleanup()` ignores Git command failures and still drops pane state. [`src/dgov/lifecycle.py:410`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:416`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:442`](src/dgov/lifecycle.py)
  `git checkout .`, `git worktree remove`, and `git worktree prune` are fire-and-forget. If removal fails, dgov can erase the DB record and orphan the worktree/branch.

- `_remove_worktree()` suppresses all Git failures everywhere it is used. [`src/dgov/gitops.py:8`](src/dgov/gitops.py)
  This helper is effectively destructive best-effort cleanup with zero error reporting.

- `_plumbing_merge()` and `_no_squash_merge()` rewrite the governor worktree while dirty-state handling is incomplete. [`src/dgov/merger.py:99`](src/dgov/merger.py), [`src/dgov/merger.py:127`](src/dgov/merger.py), [`src/dgov/merger.py:137`](src/dgov/merger.py), [`src/dgov/merger.py:175`](src/dgov/merger.py), [`src/dgov/merger.py:197`](src/dgov/merger.py), [`src/dgov/merger.py:202`](src/dgov/merger.py)
  The stash path is silent, `stash pop` errors are ignored, and the squash path issues `reset --hard HEAD` after `update-ref`. That is too destructive for a governor worktree without a transactional recovery path.

- Post-merge linting bypasses the project toolchain and amends history silently. [`src/dgov/merger.py:211`](src/dgov/merger.py), [`src/dgov/merger.py:230`](src/dgov/merger.py), [`src/dgov/merger.py:257`](src/dgov/merger.py)
  It shells out to `ruff` directly instead of `uv run ruff`, ignores formatter return codes, and unconditionally amends the merge commit. That is an implicit history rewrite in a library call.

- `wait_for_slugs()` does not use the same completion semantics as `wait_worker_pane()`. [`src/dgov/waiter.py:298`](src/dgov/waiter.py), [`src/dgov/batch.py:354`](src/dgov/batch.py), [`src/dgov/review_fix.py:232`](src/dgov/review_fix.py), [`src/dgov/review_fix.py:320`](src/dgov/review_fix.py)
  It does not pass `stable_seconds`, does not classify outcomes, and does not invoke auto-retry. Batch mode and review-fix therefore have weaker done detection than the single-pane wait path.

- `_is_done()` treats “any commit after base” as completion, even if the worker is still running. [`src/dgov/waiter.py:134`](src/dgov/waiter.py), [`src/dgov/waiter.py:145`](src/dgov/waiter.py)
  A long-running agent that checkpoints early gets marked `done` on the first commit.

- `_is_done()` treats a dead pane with no done file and no commits as `abandoned` immediately. [`src/dgov/waiter.py:151`](src/dgov/waiter.py), [`src/dgov/waiter.py:157`](src/dgov/waiter.py)
  There is no grace period, no check of the persistent log, and no attempt to distinguish “shell exited before wrapper wrote sentinel” from a genuinely abandoned task.

- `_poll_once()` can spam `pane_blocked` events on every poll cycle. [`src/dgov/waiter.py:280`](src/dgov/waiter.py), [`src/dgov/waiter.py:290`](src/dgov/waiter.py)
  Cooldown only suppresses `auto_respond()`. The fallback `pane_blocked` event still fires repeatedly for the same prompt.

- Dependency checks ignore `project_root`. [`src/dgov/preflight.py:163`](src/dgov/preflight.py), [`src/dgov/preflight.py:461`](src/dgov/preflight.py)
  `check_deps()` and `_fix_deps()` run `uv sync` in the current working directory, not the target repo passed into preflight.

- The bare governor launch path splits shell commands unsafely. [`src/dgov/cli.py:149`](src/dgov/cli.py), [`src/dgov/cli.py:152`](src/dgov/cli.py)
  `cmd.split()` will misparse quoted arguments and paths with spaces. Outside tmux the same command is sent as a raw shell string, so behavior differs by launch mode.

- `tmux.start_logging()` is shell-injection prone for paths with spaces or metacharacters. [`src/dgov/tmux.py:217`](src/dgov/tmux.py)
  `pipe-pane` runs `cat >> {log_file}` without quoting.

- `review_worker_pane()` only filters `CLAUDE.md` out of the uncommitted check, not the whole protected-file set. [`src/dgov/inspection.py:77`](src/dgov/inspection.py), [`src/dgov/persistence.py:169`](src/dgov/persistence.py)
  `.napkin.md`, `THEORY.md`, and `ARCH-NOTES.md` are still treated as user changes there even though merge protection is broader.

- `escalate_worker_pane()` hardcodes `new_slug = f"{slug}-esc"` and has no collision strategy. [`src/dgov/recovery.py:42`](src/dgov/recovery.py)
  Repeated escalations or a pre-existing `-esc` slug will collide.

- `review_fix` runs the full test suite after every merged fix. [`src/dgov/review_fix.py:331`](src/dgov/review_fix.py), [`src/dgov/review_fix.py:335`](src/dgov/review_fix.py)
  That is slow, violates the project’s own testing guidance, and amplifies flake risk.

### Testing gaps

- Integration coverage is thin for a 9,475-line package. [`tests/test_integration.py:1`](tests/test_integration.py)
  The dedicated end-to-end file is 253 lines and covers only a happy-path lifecycle, one retry path, and one conflict path.

- The integration fixture uses a mock backend that reports panes dead immediately and patches hooks out completely. [`tests/test_integration.py:53`](tests/test_integration.py), [`tests/test_integration.py:62`](tests/test_integration.py)
  That does not approximate `TmuxBackend` behavior well enough to validate waiter/lifecycle interactions.

- The default backend fixture in the largest pane test file is a loose `MagicMock`, not a protocol-faithful fake. [`tests/test_dgov_panes.py:36`](tests/test_dgov_panes.py)
  It does not model `capture_output()` failure semantics, `bulk_info()` shape drift, timing, or current-command transitions.

- The “mock backend” examples also simplify the real backend contract. [`tests/test_backend.py:119`](tests/test_backend.py)
  Those tests assert protocol shape, not behavior under tmux-style failures or race windows.

- Batch tests bypass the real completion model. [`tests/test_batch_dag.py:269`](tests/test_batch_dag.py)
  `run_batch()` is exercised with `_is_done` patched to always return `True`, so the weakest part of the batch path is not under pressure.

- Review-fix tests also bypass the real completion model and subprocess boundaries. [`tests/test_review_fix.py:223`](tests/test_review_fix.py), [`tests/test_review_fix.py:311`](tests/test_review_fix.py)
  They patch `create_worker_pane`, `_is_done`, merge, and the test runner, so pipeline structure is covered but not runtime behavior.

- There is no direct test for `wait_for_slugs()` stability semantics, blocked detection, or retry behavior. [`src/dgov/waiter.py:298`](src/dgov/waiter.py)
  The higher-level tests target `wait_worker_pane()` or patch `_is_done()` entirely.

- There is no direct test for unknown-agent behavior in the library APIs. [`src/dgov/lifecycle.py:136`](src/dgov/lifecycle.py), [`src/dgov/lifecycle.py:478`](src/dgov/lifecycle.py)
  CLI tests reject bad agent IDs, but the callable surface is unguarded.

- There is no concurrency test for event journaling or state/event ordering. [`src/dgov/persistence.py:53`](src/dgov/persistence.py), [`src/dgov/persistence.py:351`](src/dgov/persistence.py)
  The suite stress-tests `add_pane()` only.

## 2. Breaking changes needed

- Replace `dgov.panes` as a private-helper dump with an explicit public API. Keep only supported public names there, or remove the shim entirely. This breaks imports of `_trigger_hook`, `_is_done`, `_full_cleanup`, `_compute_freshness`, and similar internals currently leaked via [`src/dgov/panes.py:7`](src/dgov/panes.py).

- Introduce typed result models for pane lifecycle, merge, review, retry, batch, and wait operations. The current dict-returning APIs in [`src/dgov/inspection.py:18`](src/dgov/inspection.py), [`src/dgov/merger.py:547`](src/dgov/merger.py), [`src/dgov/recovery.py:18`](src/dgov/recovery.py), [`src/dgov/waiter.py:320`](src/dgov/waiter.py), and [`src/dgov/batch.py:283`](src/dgov/batch.py) need a stable schema if 0.9.0 is going to be consumable as a library.

- Standardize the root/session contract. Every public orchestration API should either take a `SessionContext` object or a consistent `(project_root, session_root)` pair. The current mixed signatures across [`src/dgov/lifecycle.py:136`](src/dgov/lifecycle.py), [`src/dgov/waiter.py:298`](src/dgov/waiter.py), [`src/dgov/waiter.py:460`](src/dgov/waiter.py), and [`src/dgov/persistence.py:319`](src/dgov/persistence.py) should be broken once, not papered over.

- Make unknown agents a hard error in the library surface. `create_worker_pane()` and `resume_worker_pane()` should raise a typed exception if the agent is missing from the loaded registry. That is a behavior break from the current silent-no-launch path at [`src/dgov/lifecycle.py:172`](src/dgov/lifecycle.py) and [`src/dgov/lifecycle.py:524`](src/dgov/lifecycle.py).

- Remove project-local ability to define executable agent commands unless there is an explicit trust model. The current behavior in [`src/dgov/agents.py:306`](src/dgov/agents.py) should be broken either toward “project config may only select from user/global agents” or toward an explicit `allow_project_commands = true` escape hatch.

- Replace “merge may mutate and amend the governor worktree” with an explicit merge transaction API. `_lint_fix_merged_files()` and the stash/reset logic in [`src/dgov/merger.py:99`](src/dgov/merger.py), [`src/dgov/merger.py:127`](src/dgov/merger.py), and [`src/dgov/merger.py:257`](src/dgov/merger.py) should not remain implicit side effects of `merge_worker_pane()`.

- Remove `review_fix`’s full-suite validation behavior. The current implicit `uv run pytest -q --tb=short -x` in [`src/dgov/review_fix.py:335`](src/dgov/review_fix.py) should become a caller-supplied targeted validation plan.

## 3. Module restructure proposal

Proposed package shape for 0.9.0:

- `src/dgov/api/`
  Public typed entrypoints only: pane lifecycle, waiting, merge, review, retry, batch.

- `src/dgov/types.py`
  `SessionContext`, `PaneRecord`, `PaneStatus`, `WaitResult`, `ReviewResult`, `RetryResult`, `BatchResult`, `OpenRouterStatus`.

- `src/dgov/state/store.py`
  SQLite connection management and pane/event persistence only.

- `src/dgov/state/machine.py`
  State constants, transition validation, typed transition errors.

- `src/dgov/runtime/worktrees.py`
  Worktree creation/removal and branch management.

- `src/dgov/runtime/launcher.py`
  Shared pane-launch pipeline used by both create and resume.

- `src/dgov/runtime/cleanup.py`
  Resource teardown with transactional reporting instead of silent best-effort cleanup.

- `src/dgov/runtime/completion.py`
  Done detection, blocked detection, and waiter logic. `wait_for_slugs()` and `wait_worker_pane()` should share the same engine.

- `src/dgov/runtime/responders.py`
  Auto-response rules and blocked-prompt handling.

- `src/dgov/merge/plumbing.py`
  Pure Git merge operations.

- `src/dgov/merge/conflicts.py`
  Conflict prediction and agent-assisted/manual resolution.

- `src/dgov/merge/post_merge.py`
  Protected-file verification, lint/test hooks, and cleanup policy.

- `src/dgov/commands/`
  Split Click commands by area: `panes.py`, `governor.py`, `admin.py`, `templates.py`, `openrouter.py`, `experiments.py`.

Concrete splits:

- Split [`src/dgov/lifecycle.py`](src/dgov/lifecycle.py) into `runtime/worktrees.py`, `runtime/launcher.py`, and `runtime/cleanup.py`.
- Split [`src/dgov/waiter.py`](src/dgov/waiter.py) into `runtime/completion.py` and `runtime/interaction.py`.
- Split [`src/dgov/merger.py`](src/dgov/merger.py) into `merge/plumbing.py`, `merge/conflicts.py`, and `merge/post_merge.py`.
- Split [`src/dgov/persistence.py`](src/dgov/persistence.py) into `state/store.py` and `state/machine.py`.
- Replace [`src/dgov/panes.py`](src/dgov/panes.py) with either a thin public facade or remove it entirely.
- Split [`src/dgov/cli.py`](src/dgov/cli.py) into command modules plus service-layer calls.

## 4. Testing roadmap

Minimum coverage to ship 0.9.0 safely:

- Add a real `FakeBackend` test fixture that matches `WorkerBackend` semantics, including liveness transitions, `capture_output()` failures, `current_command()`, and `bulk_info()` snapshots. Replace the loose `MagicMock` fixtures in [`tests/test_dgov_panes.py:36`](tests/test_dgov_panes.py) and [`tests/test_integration.py:53`](tests/test_integration.py).

- Add direct tests for `wait_for_slugs()` covering:
  done-signal completion, exit-file failure, commit-based false positive, stable-output completion, blocked prompt detection, and auto-retry parity with `wait_worker_pane()`. The current batch/review-fix tests do not cover this path.

- Add SQLite concurrency tests for `emit_event()`, `update_pane_state()`, and mixed `add_pane()+emit_event()` workloads. Current concurrency coverage stops at [`tests/test_concurrent_workers.py:164`](tests/test_concurrent_workers.py).

- Add failure-injection tests for cleanup ordering:
  `worktree remove` fails, branch delete fails, tmux destroy fails, and cleanup after successful merge fails. dgov should not claim `closed` or `merged` until teardown is known.

- Add library-level tests for unknown agents on create/resume. The CLI rejects bad input; the library does not.

- Add targeted integration tests for:
  dirty governor worktree during merge, stash-pop conflict after merge, repeated escalation slug collisions, project paths with spaces, and hook failure propagation.

- Add a real batch integration test with staggered completion and one TUI-like worker that never exits cleanly. Today batch coverage is almost entirely structural/mocked.

- Add a review-fix integration test that validates targeted test selection instead of patching subprocesses. The current unit tests mostly prove JSON plumbing, not operational safety.

## 5. Priority ordering

1. Fix completion semantics and cleanup ordering first.
   Files: [`src/dgov/waiter.py`](src/dgov/waiter.py), [`src/dgov/lifecycle.py`](src/dgov/lifecycle.py), [`src/dgov/merger.py`](src/dgov/merger.py), [`src/dgov/gitops.py`](src/dgov/gitops.py)
   Reason: these are the highest blast-radius paths. They control whether dgov decides work is done, whether it merges too early, and whether it leaves orphaned state behind.

2. Lock down the public API and root/session contract.
   Files: [`src/dgov/panes.py`](src/dgov/panes.py), [`src/dgov/lifecycle.py`](src/dgov/lifecycle.py), [`src/dgov/waiter.py`](src/dgov/waiter.py), [`src/dgov/recovery.py`](src/dgov/recovery.py), [`src/dgov/inspection.py`](src/dgov/inspection.py)
   Reason: 0.9.0 is the right time to break callers once instead of preserving accidental internals.

3. Untangle `persistence.py` and harden SQLite/event behavior.
   Files: [`src/dgov/persistence.py`](src/dgov/persistence.py)
   Reason: this is the state backbone. The current split between retried pane writes and non-retried event writes is an avoidable footgun.

4. Split `lifecycle.py`, `waiter.py`, `merger.py`, and `cli.py`.
   Files: [`src/dgov/lifecycle.py`](src/dgov/lifecycle.py), [`src/dgov/waiter.py`](src/dgov/waiter.py), [`src/dgov/merger.py`](src/dgov/merger.py), [`src/dgov/cli.py`](src/dgov/cli.py)
   Reason: the coupling is already producing drift and duplicated bug surfaces.

5. Fix the configuration trust boundary.
   Files: [`src/dgov/agents.py`](src/dgov/agents.py)
   Reason: this is a latent security and supply-chain problem, but it is less likely to corrupt live sessions than the lifecycle issues above.

6. Expand integration coverage after the architectural cuts.
   Files: [`tests/test_integration.py`](tests/test_integration.py), [`tests/test_concurrent_workers.py`](tests/test_concurrent_workers.py), new targeted files
   Reason: testing the current architecture more deeply before stabilizing the API will create churn.

## 6. Hard problems

- Defining one authoritative completion model.
  Right now dgov mixes filesystem sentinels, branch commits, tmux liveness, and output stability. 0.9.0 needs a clear precedence model and probably agent/backend capability flags instead of one heuristic path for all agents.

- Making merge operations transactional from the governor’s point of view.
  The current implementation mutates the governor checkout, stashes user work, and sometimes amends commits. A safer design may require an isolated merge worktree or a reversible merge transaction model.

- Separating “session state” from “runtime resources”.
  Pane state, events, tmux pane titles, worktree directories, and branch refs are not currently updated as one unit. Designing an explicit resource lifecycle will be non-trivial.

- Deciding the trust model for project-local config.
  Either repository config is trusted and can launch arbitrary commands, or it is not. The current middle ground in [`src/dgov/agents.py:311`](src/dgov/agents.py) is incoherent.

- Preserving CLI ergonomics while breaking the accidental library API.
  `dgov.panes` is currently a compatibility blanket. Replacing it without recreating the same coupling will require a deliberate facade design.
