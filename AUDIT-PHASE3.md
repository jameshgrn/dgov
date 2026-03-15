# Phase 3 Audit

Passing tests are not the same thing as release confidence. Phase 1 and Phase 2 removed obvious API ambiguity and some reliability hazards, but the codebase still has destructive paths and false-green workflows that are too risky for a stable `0.9.0`.

## 1. What Phase 1 and 2 fixed vs. what remains unfixed

### What Phase 1 and 2 actually fixed

- Import indirection was reduced: the `panes.py` facade is gone, so callers now hit real modules directly.
- Lifecycle duplication was reduced: shared launcher logic moved into `src/dgov/lifecycle.py`, which lowered the odds of launcher drift.
- Invalid agent IDs now fail fast instead of falling through to partial side effects.
- The config trust boundary improved: project-local TOML can no longer inject commands or env into agent definitions.
- State transitions became explicit enough to support metrics, stats, retry lineage, and worker context propagation.
- Cleanup ordering improved in some paths, but not all paths.

### What is still unfixed

- The close path still contains a built-in data-loss operation (`git checkout .`) before forced worktree removal (`src/dgov/lifecycle.py:464-470`).
- The `closed` terminal state is still not durable in the normal close path because the pane row is removed before the state transition is attempted (`src/dgov/lifecycle.py:495-499`, `src/dgov/lifecycle.py:513-521`, `src/dgov/persistence.py:393-399`).
- Merge still mutates the governor worktree through stash/update-ref/reset-hard/pop instead of staying transactional (`src/dgov/merger.py:99-139`).
- Post-merge fallback still allows protected-file damage onto main and only emits a warning (`src/dgov/merger.py:657-680`).
- Review-fix still treats malformed review output as "no findings" and can merge fixes before meaningful validation (`src/dgov/review_fix.py:70-110`, `src/dgov/review_fix.py:328-377`).
- Preflight/status still rely on heuristic git scans that are both expensive and incomplete for overlap detection (`src/dgov/preflight.py:267-320`, `src/dgov/status.py:27-96`).
- The largest orchestration modules are still large enough that behavior changes have more blast radius than necessary: `src/dgov/cli.py` is 1643 lines, `src/dgov/merger.py` 734, `src/dgov/lifecycle.py` 667, `src/dgov/dashboard.py` 657.

## 2. Technical debt inventory

### P0: data loss and state corruption risks

1. Forced cleanup discards worker edits by design.
   - `src/dgov/lifecycle.py:464-470`
   - This runs `git checkout .` in the worker worktree before `git worktree remove --force`.
   - That is not "cleanup"; it is destructive rollback.
   - Worse, the current test suite codifies it as intended behavior instead of flagging it as unsafe: `tests/test_dgov_panes.py:1161-1208`.

2. `closed` state is silently dropped in the normal close path.
   - `src/dgov/lifecycle.py:495-499`
   - `src/dgov/lifecycle.py:513-521`
   - `src/dgov/persistence.py:393-399`
   - `_full_cleanup()` removes the pane row, then `close_worker_pane()` calls `update_pane_state(..., "closed")`. On a missing row, `update_pane_state()` becomes a no-op.
   - Result: events may say "closed", but durable pane state does not. That undermines auditability and any future recovery/reporting logic.

3. Squash merge still rewrites the governor worktree with a stash/update-ref/reset-hard sequence.
   - `src/dgov/merger.py:99-139`
   - `git reset --hard HEAD` after ref advancement is a high-blast-radius operation in the main repo.
   - The stash/pop wrapper softens the common case, but it is not transactional. If stash-pop or reset fails, the branch may already have advanced.

4. Post-merge fallback permits protected-file damage to land on main.
   - `src/dgov/merger.py:657-680`
   - If `post_merge` hook does not run, protected files changed after merge only produce a warning.
   - The current tests explicitly accept this degraded behavior: `tests/test_merger_coverage.py:616-724`.

### P1: false confidence and weak orchestration semantics

5. Post-merge lint rewrites merge history by amending the merge commit.
   - `src/dgov/merger.py:247-264`
   - A merge should represent what the worker produced. Silent amend-on-merge hides post-processing inside the same commit and complicates blame/recovery.

6. Review-fix parser collapses malformed review output into "no findings".
   - `src/dgov/review_fix.py:70-110`
   - No JSON array found or JSON parse failure returns `[]`.
   - That means transport noise, truncation, or prompt drift looks identical to "the review found nothing."

7. Review-fix merges before validation and does not roll back failing merges.
   - `src/dgov/review_fix.py:328-377`
   - The pipeline merges each fix branch, then runs guessed tests, and on test failure only records the slug in `test_failures`.
   - There is no revert, no reset, and no quarantine. A failed validation still leaves main changed.

8. Review-fix test targeting is guesswork, not evidence.
   - `src/dgov/review_fix.py:348-377`
   - It maps `src/.../foo.py` to at most one of `tests/test_foo.py` or `tests/test_<pkg>_foo.py`.
   - That misses integration tests, markers, renamed tests, multi-module effects, and non-`src` changes.

9. Batch execution mixes task IDs with runtime slugs.
   - `src/dgov/batch.py:311-388`
   - Failure propagation is keyed by `task["id"]` in some places and by `slug` in others.
   - It works only because the code currently forces `slug == task["id"]`. That is a latent logic bug, not a stable contract.

10. DAG cycle reporting is too weak to debug real specs.
    - `src/dgov/batch.py:180-183`
    - It raises `Dependency cycle detected: node`, not the actual cycle path.
    - This is not cosmetic. Batch specs are operator-facing, and low-quality errors slow recovery.

### P2: incomplete detection and brittle config handling

11. File-lock preflight checks the wrong thing.
    - `src/dgov/preflight.py:267-320`
    - It runs `git diff --name-only HEAD` inside each worktree, which only reflects working-tree changes relative to HEAD.
    - It misses committed-but-unmerged worker edits, and it treats overlap detection as a local diff heuristic instead of branch-vs-base analysis.

12. Freshness/status computation is expensive and heuristic-heavy.
    - `src/dgov/status.py:27-96`
    - Up to three git subprocesses per pane for a soft signal (`fresh`, `warn`, `stale`) is expensive for dashboard and status refresh loops.
    - The thresholds are also arbitrary enough that users may over-trust them.

13. Orphan pruning can delete recoverable worktrees without proving they are truly orphaned.
    - `src/dgov/status.py:258-274`
    - Pass 2 only checks whether the directory is referenced in state, then calls `_remove_worktree()`.
    - That is unsafe if state is stale/corrupt or if a live backend/session exists without a matching row.

14. Project config writes are not round-trip safe.
    - `src/dgov/agents.py:390-422`
    - `write_project_config()` manually rewrites TOML as flat string assignments.
    - Any non-string value, nested table, or formatting/comment structure in other sections is at risk of corruption.

## 3. Module split recommendations

Do not start Phase 3 with file splitting. Start with behavior fixes. Split only where it reduces the risk of those fixes.

### Split `src/dgov/cli.py` last, but split it decisively

Current problem:
- `src/dgov/cli.py` is 1643 lines and mixes command registration, argument parsing, JSON formatting, and direct orchestration calls.
- The pane command surface alone spans `src/dgov/cli.py:195-919`.

Recommended split:
- `src/dgov/cli/__init__.py`
  - keep `cli` root group and governor-context check only
- `src/dgov/cli/pane.py`
  - move pane commands from `src/dgov/cli.py:195-919`
- `src/dgov/cli/admin.py`
  - move `preflight`, `status`, `rebase`, `agents`, `version`, `stats`, `dashboard`, `init`, `doctor`
  - source ranges: `src/dgov/cli.py:931-1129`, `src/dgov/cli.py:1437-1540`
- `src/dgov/cli/templates.py`
  - move template group from `src/dgov/cli.py:1132-1197`
- `src/dgov/cli/batch.py`
  - move checkpoint + batch commands from `src/dgov/cli.py:1198-1247`
- `src/dgov/cli/experiment.py`
  - move experiment group from `src/dgov/cli.py:1248-1344`
- `src/dgov/cli/review_fix.py`
  - move `review-fix` command from `src/dgov/cli.py:1345-1388`
- `src/dgov/cli/openrouter.py`
  - move OpenRouter group from `src/dgov/cli.py:1390-1434`

Rule:
- No helper graveyard.
- Each submodule should expose Click commands only, not shared orchestration utilities.

### Split `src/dgov/lifecycle.py` before making more behavior changes there

Current problem:
- One file owns worktree creation, tmux launch, hooks, cleanup, close, and resume.
- The destructive cleanup bug and the state-ordering regression are in the same file, which raises regression risk.

Recommended split:
- `src/dgov/lifecycle_launch.py`
  - `_setup_and_launch_agent`
  - prompt rewriting, env export, logging, hook dispatch
  - source anchor: `src/dgov/lifecycle.py:150-267`
- `src/dgov/lifecycle_create.py`
  - `create_worker_pane`
  - worktree creation, base SHA capture, concurrency/health checks
  - source anchor: `src/dgov/lifecycle.py:271-415`
- `src/dgov/lifecycle_cleanup.py`
  - `_full_cleanup`
  - `close_worker_pane`
  - source anchor: `src/dgov/lifecycle.py:417-529`
- `src/dgov/lifecycle_resume.py`
  - `resume_worker_pane`
  - source anchor: `src/dgov/lifecycle.py:532-667`

Rule:
- Cleanup stays separate from launch. Do not let one function own both creation and teardown semantics.

### Split `src/dgov/merger.py` before expanding merge behavior

Current problem:
- `src/dgov/merger.py` currently owns plumbing merge, no-squash merge, conflict detection, AI conflict resolution, worktree auto-commit, protected-file restoration, post-merge lint, and public orchestration.
- That is too much mutable behavior in one file for a release-critical path.

Recommended split:
- `src/dgov/merge_plumbing.py`
  - `_plumbing_merge`
  - `_no_squash_merge`
  - `_detect_conflicts`
  - source anchors: `src/dgov/merger.py:20-205`, `src/dgov/merger.py:277-302`
- `src/dgov/merge_resolution.py`
  - `_pick_resolver_agent`
  - `_resolve_conflicts_with_agent`
  - source anchors: `src/dgov/merger.py:305-440`
- `src/dgov/merge_postprocess.py`
  - `_commit_worktree`
  - `_restore_protected_files`
  - `_lint_fix_merged_files`
  - source anchors: `src/dgov/merger.py:211-271`, `src/dgov/merger.py:443-543`
- `src/dgov/merge.py`
  - `merge_worker_pane` only
  - source anchor: `src/dgov/merger.py:548-734`

Rule:
- Keep `merge_worker_pane()` as the coordinator. Do not scatter merge policy across unrelated modules.

### Dashboard split is optional for 0.9.0

`src/dgov/dashboard.py` is large, but it is not where the release risk lives.

If it is touched, split it into:
- `src/dgov/dashboard_state.py`
  - `DashboardState`, `fetch_panes`, `fetch_detail`, `data_thread`
- `src/dgov/dashboard_render.py`
  - draw helpers
- `src/dgov/dashboard_actions.py`
  - `_show_diff`, `_show_capture`, `_execute_action`, main loop

Do this only after the destructive lifecycle/merge paths are fixed.

## 4. Testing gaps that block a stable 0.9.0 release

The suite is broad, but some of the most dangerous semantics are either untested or tested in a way that entrenches unsafe behavior.

1. There is no release-grade test that forbids destructive cleanup of worker edits.
   - Current suite does the opposite and locks in `git checkout .` before removal: `tests/test_dgov_panes.py:1161-1208`.
   - A stable release needs a test that proves close/cleanup preserves user data unless the user explicitly chose destructive behavior with a documented contract.

2. There is no integration test for dirty governor merge failure modes.
   - Missing cases:
   - stash push fails
   - update-ref succeeds but reset fails
   - stash pop conflicts after merge
   - post-merge lint amends a commit while the governor tree is dirty

3. There is no test proving `closed` state durability after cleanup.
   - Integration coverage stops at "close returns True" or "merge cleaned it up": `tests/test_integration.py:76-133`.
   - What is missing is a state/event audit test that verifies close leaves an unambiguous durable terminal record.

4. Review-fix coverage is too mocked to trust for release gating.
   - `tests/test_review_fix.py:222-468`
   - Missing cases:
   - malformed review output must fail the pipeline, not return zero findings
   - post-merge targeted tests fail and the merge is rolled back or quarantined
   - multi-file findings trigger the correct fix fan-out and merge ordering

5. Preflight overlap detection is not tested against committed worker changes.
   - Current preflight tests are strong on unit branches, but they do not prove the lock detector catches a worker branch that already committed conflicting edits and is waiting to merge.

6. Protected-file enforcement still lacks a red-line integration test.
   - Current coverage accepts warning-only behavior after merge: `tests/test_merger_coverage.py:616-665`.
   - Stable release coverage should require the merge to fail or auto-revert when protected files differ after merge.

7. Dashboard and CLI tests are mostly wrapper tests, not orchestration tests.
   - `tests/test_dgov_cli.py:451-646`
   - `tests/test_dashboard_smoke.py:361-503`
   - That is fine for smoke coverage, but it does not validate stateful side effects across backend, state DB, and git.

## 5. Priority ordering with rationale

### P0: eliminate destructive behavior

1. Remove forced cleanup data loss and make merge transactional.
   - Rationale: nothing else matters if a close or merge can silently discard work or mutate main in a non-recoverable way.

2. Make terminal state and event history truthful.
   - Rationale: once destructive behavior is reduced, the next requirement is being able to trust what happened. Today `closed` is not durable enough.

### P1: make automation trustworthy

3. Fix review-fix so malformed review output is a hard failure, not a silent clean bill of health.
   - Rationale: false negatives are worse than visible failures in an AI-driven orchestration tool.

4. Replace heuristic post-merge validation with explicit release-grade validation semantics.
   - Rationale: merging before meaningful validation is the wrong order for a stable release workflow.

5. Harden preflight/status overlap detection.
   - Rationale: the tool claims to orchestrate parallel workers safely. If overlap detection is wrong, users will hit preventable merge pain.

### P2: reduce change risk in the implementation

6. Split `lifecycle.py` and `merger.py` around the boundaries above.
   - Rationale: those splits directly reduce regression risk while fixing P0/P1 behavior.

7. Split `cli.py` after the behavioral fixes settle.
   - Rationale: the CLI file is too large, but splitting it first would mostly reshuffle code while leaving the highest-risk semantics untouched.

## 6. What NOT to do

- Do not start Phase 3 with aesthetic refactors.
  - Splitting files before fixing destructive semantics will create churn without reducing user risk.

- Do not add a generic plugin or orchestration framework.
  - The current problem is unsafe behavior in a few concrete paths, not lack of an abstraction layer.

- Do not add more auto-healing that is not transactional.
  - Auto-linting, auto-merge, auto-fix, and auto-resolve are only acceptable when failure leaves the repo in a provable, recoverable state.

- Do not preserve old module paths with new compatibility shims.
  - Phase 1 already paid down one shim. Keep imports honest and move callers directly.

- Do not add config flags for every unsafe edge case.
  - The right fix for data loss is safer default behavior, not a matrix of toggles.

- Do not treat the dashboard as a release blocker.
  - It is large, but it is not where the blast radius is. Fix lifecycle, merge, and review correctness first.

## Bottom line

Phase 1 and Phase 2 made the codebase less ambiguous. They did not yet make it safe enough to call stable. The Phase 3 roadmap should be centered on three things:

- no silent data loss
- no false-green automation
- no ambiguous state history

If those are fixed, module splitting becomes worthwhile. If those are not fixed, a `0.9.0` label is marketing, not engineering.
