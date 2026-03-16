# Review: DAG Runner Design

Reviewer: Hunter (review-dag-runner worktree)
Date: 2026-03-16
Files reviewed: DESIGN-DAG-RUNNER.md, DAG-DAG-RUNNER.md, lifecycle.py, waiter.py, merger.py (lines 588-777), inspection.py, recovery.py, persistence.py

---

## 1. API CORRECTNESS

All five core function signatures match the actual source code. Verified via direct read of each module.

| Function | Design uses | Actual signature | Match? |
|----------|-------------|------------------|--------|
| `create_worker_pane` | `(project_root, prompt, agent, slug, session_root, ...)` | `(project_root, prompt, agent, permission_mode, slug, env_vars, extra_flags, session_root, ...)` | ✅ (keyword args) |
| `wait_worker_pane` | `(..., auto_retry=False)` | `(project_root, slug, session_root, timeout, poll, stable, auto_retry)` | ✅ |
| `review_worker_pane` | `(project_root, slug, session_root, full)` | `(project_root, slug, session_root, full)` | ✅ |
| `merge_worker_pane` | `(project_root, slug, session_root, resolve, squash)` | `(project_root, slug, session_root, resolve, squash)` | ✅ |
| `close_worker_pane` | `(project_root, slug, session_root)` | `(project_root, slug, session_root, force)` | ✅ |
| `retry_worker_pane` | `(project_root, slug, session_root, agent, prompt, permission_mode)` | `(project_root, slug, session_root, agent, prompt, permission_mode)` | ✅ |
| `escalate_worker_pane` | `(project_root, slug, target_agent, session_root, permission_mode)` | `(project_root, slug, target_agent, session_root, permission_mode)` | ✅ |

**Finding 1.1 (Low):** The design doc does not mention `permission_mode` in the `create_worker_pane` call path. The `DagTaskSpec` dataclass has `permission_mode`, so the plumbing is there, but the "Governor-Side Call Path" section omits it from the bullet list. Not a bug, just incomplete documentation.

**Finding 1.2 (Low):** `retry_worker_pane` signature includes `permission_mode` but the design's escalation policy section never discusses permission mode changes across retries. If a task uses `acceptEdits` and escalates to claude (which defaults to `bypassPermissions`), the DAG runner would pass through whatever the task spec says. This is fine, but worth noting the default mismatch is handled by the task schema, not hardcoded.

---

## 2. COMPLETENESS

**Finding 2.1 (Critical): Orphan pane handling on governor death is underspecified.**

The design says "resume from SQLite state" but does not address what happens to panes that were dispatched but not yet recorded. Scenario:

1. Governor dispatches task T1 via `create_worker_pane()`.
2. Governor dies before writing the `dag_tasks` row.
3. On restart, DAG runner sees T1 as `pending` and dispatches it again.
4. Now two panes exist for T1 with different worktrees and branches.

The `create_worker_pane` function does write to `state.db` (via `add_pane`), but it also has a cleanup path on exception (`_full_cleanup`). The issue is: if the governor dies *after* `add_pane` but *before* the DAG runner writes its own `dag_tasks` row, you have an orphan pane with no DAG tracking.

**Fix:** On resume, the DAG runner should scan `state.db` for panes whose slug matches the current DAG run's task slugs (or escalation-derived slugs) and reconcile: either adopt them into the current run or close them before re-dispatching.

**Finding 2.2 (High): No `dgov dag merge` subcommand for `--no-auto-merge` workflow.**

The design says `--no-auto-merge` persists the DAG run as `awaiting_merge`, but there is no CLI command to actually perform the deferred merge. The operator would have to manually call `dgov pane merge` for each `reviewed_pass` pane, which defeats the purpose of tracking merge order.

**Fix:** Add `dgov dag merge <dagfile>` that reads the persisted run, identifies `reviewed_pass` tasks, and merges them in canonical topological order. Or document that `dgov dag run` with `--no-auto-merge` followed by `dgov dag run` (without the flag) will pick up and merge.

**Finding 2.3 (Medium): No spec for what happens when `--skip` causes transitive skip of already-dispatched tasks.**

If T2 depends on T1, and T1 is skipped, the design says "transitively skip its dependents." But what if T2 has already been dispatched (e.g., T1 failed mid-tier and T2 was running in parallel in the same tier)? The DAG runner would need to close T2's pane.

**Fix:** Document that skip propagation must close any already-dispatched panes for skipped dependents. The execution loop should check for newly-skippable tasks after each review/failure, not just at tier start.

**Finding 2.4 (Medium): `_augment_prompt_with_review` is specified in T3 but not described anywhere.**

T3 lists `_augment_prompt_with_review(...) -> str` as an implementation helper, but neither the design doc nor the task spec explains what review feedback gets injected into the retry prompt. The `retry_context` function in `retry.py` reads log tails and exit codes — is that what should be used, or should the DAG runner inject the `review_worker_pane` issues list?

**Fix:** Specify the prompt augmentation format. Suggested: include `review_result["issues"]` as bullet points plus the log tail from `retry_context`.

**Finding 2.5 (Low): `commit_message` is in the schema but never referenced in the execution engine.**

The design has `commit_message` as a required field per task, and the example TOML includes it, but nowhere in the execution loop or dataclass plumbing does it get used. Workers receive their prompt, which presumably contains the commit intent, but `commit_message` itself is not passed to any API.

**Fix:** Either document that `commit_message` is advisory/metadata only, or specify how it's injected (e.g., appended to the prompt, or used as a post-hoc validation check on the commit log).

---

## 3. DAG TASK QUALITY

**Finding 3.1 (High): T2 (Execution engine core) is too large for a single hunter task.**

T2 requires:
- `run_dag()` entry point with 5 keyword args
- 6 private helpers (`_start_or_resume_run`, `_dispatch_task`, `_wait_for_tier`, `_review_passed_task`, `_merge_tasks_in_order`, `_finalize_run`)
- Integration with 4 external APIs (`create_worker_pane`, `wait_worker_pane`, `review_worker_pane`, `merge_worker_pane`)
- State transition logic (pending → dispatched → waiting → reviewing → reviewed_pass/merged/failed)
- Merge ordering enforcement
- Dry-run rendering
- Tests covering dry-run, single-tier, multi-tier, tier limiting, no-auto-merge

That's roughly 400-600 lines of non-trivial orchestration code plus tests. A free model (hunter/Qwen 35B) will likely stall or produce incomplete code.

**Fix:** Split T2 into:
- **T2a**: Core execution loop (dispatch, wait, review, merge for a single tier) — depends on T1, T5, T6
- **T2b**: Multi-tier orchestration + dry-run + options — depends on T2a

**Finding 3.2 (Medium): T1 combines new pure functions with batch.py refactor.**

T1 asks the worker to implement 5 new functions AND refactor `batch.py` to import them while preserving all existing behavior. The refactor introduces regression risk. If the worker breaks `batch.py`, the existing batch workflow is broken.

**Fix:** Split into T1a (pure DAG helpers) and T1b (batch.py refactor + test_batch_dag.py). Or make T1b a separate task after T1a merges.

**Finding 3.3 (Low): T3 escalation logic duplicates `maybe_auto_retry` from retry.py.**

The design says "handle these rules explicitly" in T3, but `retry.py` already has `maybe_auto_retry` that does retry-with-backoff and escalation. The DAG runner needs different behavior (it owns attempts, not the auto-retry engine), but the task spec doesn't clearly delineate what to reuse vs. reimplement.

**Fix:** Add a note: "Do NOT call `maybe_auto_retry`. The DAG runner implements its own attempt loop. You may reuse `retry_context` from `retry.py` for prompt augmentation."

---

## 4. FORMAT CHOICE

**Finding 4.1 (Pass): TOML is well-justified.**

- Existing codebase uses TOML (`batch.py`).
- `tomllib` is stdlib in Python 3.12+ (no new dependency).
- Shape matches (scalar + list + keyed blocks).
- Comments supported.

No issues. YAML would add a dependency and implicit typing footguns. JSON lacks comments.

**Finding 4.2 (Low): No schema validation tool mentioned.**

The design validates required fields at parse time, but there's no mention of a schema definition (e.g., JSON Schema for TOML, or a `validate_dag` that checks field types). For hand-authored DAGs, bad types (e.g., `timeout_s = "nine hundred"`) would fail at runtime, not parse time.

**Fix (optional):** Add basic type checking in `_parse_task` — `timeout_s` must be int, `depends_on` must be list of strings, etc. This is low priority since the dataclass constructors will catch most type errors.

---

## 5. FAILURE MODES

**Finding 5.1 (High): Merge conflict stop is correct but partial-merge state is lost.**

The design says "stop the whole DAG immediately" on merge conflict. If T0 and T1 merged successfully, then T2 conflicts, the DAG run status becomes `failed`. But T0 and T1 are already merged into main. On resume, the DAG runner would see T0 and T1 as `merged` and skip them. This works.

However, the `state_json` stores `"merged": ["T0", "T1"]` but the `dag_tasks` table also has a status column. If these get out of sync (e.g., `state_json` says merged but `dag_tasks` says `reviewed_pass`), resume behavior is undefined.

**Fix:** Make `dag_tasks.status` the source of truth, not `state_json`. Use `state_json` only for computed/cached values (tiers, options, hash). On resume, reconstruct state from `dag_tasks` rows.

**Finding 5.2 (Medium): No handling of `merge_worker_pane` returning non-conflict errors.**

The design covers conflict (`resolve="skip"` returns `{"error": ..., "conflicts": ...}`), but `merge_worker_pane` can also return `{"error": "Pane not found: ..."}` or `{"error": "No base_sha recorded"}`. These should probably also stop the DAG, but the design only mentions conflicts.

**Fix:** Generalize: "If `merge_worker_pane` returns a dict with an `error` key, stop the DAG."

**Finding 5.3 (Medium): `review_worker_pane` doesn't update pane state — DAG runner must.**

The design correctly notes that `review_worker_pane` emits `review_pass`/`review_fail` events but does not call `update_pane_state`. The DAG runner must call `update_pane_state(session_root, slug, "reviewed_pass")` or `"reviewed_fail"` explicitly. This is documented, but there's a subtle issue: the state transition table in `persistence.py` allows `done → reviewed_pass` and `done → reviewed_fail`, but what if the pane is in `failed` state (runtime failure, not review failure)? The DAG runner might try to review a failed pane.

**Fix:** The DAG runner should skip review for panes in `failed`, `timed_out`, or `abandoned` states. Only review panes in `done` state. This is implied but not explicit.

**Finding 5.4 (Low): Timeout escalation policy is stated but timeout value propagation is unclear.**

The design says "timeout → escalate" and the task spec has `timeout_s`, but `wait_worker_pane`'s `timeout` parameter defaults to 600s. If `DagTaskSpec.timeout_s` is 900, the DAG runner must pass `timeout=900` to `wait_worker_pane`. This is straightforward but not explicitly called out in the execution loop pseudocode.

**Fix:** Add a note in T2: "Pass `timeout=task_spec.timeout_s` to `wait_worker_pane`."

---

## 6. MERGE ORDER SAFETY

**Finding 6.1 (Pass): Merge order is correct.**

Merging in canonical topological order (not worker completion order) ensures:
- Dependencies merge before dependents.
- Siblings in the same tier merge in stable slug order.
- File overlap is pre-validated by tier computation, so sequential merge within a tier has no file conflicts.

No issues with the merge ordering logic.

**Finding 6.2 (Low): `merge_worker_pane` does cleanup — design warns against double-close.**

The design correctly notes that `merge_worker_pane` calls `_full_cleanup` + `remove_pane` on success, so the DAG runner must NOT call `close_worker_pane` again. This is a subtle API contract that could easily be violated by a developer who didn't read the merger source. The T2 task spec mentions this, which is good.

---

## 7. INTEGRATION

**Finding 7.1 (Pass): Integration design is clean.**

- Calls Python APIs directly (no shelling out). ✅
- Shares `state.db` (no second SQLite file). ✅
- Extends `VALID_EVENTS` (dashboard picks them up generically). ✅
- Plans to refactor `batch.py` to share DAG helpers (avoids code drift). ✅
- `auto_retry=False` prevents invisible retry that would confuse DAG attempt tracking. ✅

**Finding 7.2 (Low): CLI registration path is unspecified.**

T4 says "Register the new command in `src/dgov/cli/__init__.py`" but the current `__init__.py` structure is not shown. If it uses Click groups, the integration is trivial. If it uses a different pattern, the worker might guess wrong.

**Fix:** Not critical — the worker will read the file first (per worker instructions).

---

## 8. ADDITIONAL FINDINGS

**Finding 8.1 (Medium): No specification for DAG file hash algorithm.**

The design says "DAG file hash" for resume validation but doesn't specify the algorithm. SHA-256 of the raw file bytes is the obvious choice, but the TOML parser might normalize whitespace or key ordering.

**Fix:** Specify: "SHA-256 of the raw file bytes, computed before parsing."

**Finding 8.2 (Low): Event payload for `dag_task_escalated` includes `reason` but the reason codes are not in `VALID_EVENTS`.**

The design lists reason codes (`health_check_failed`, `timeout`, `zero_commit`, `review_failed`, `runtime_failed`) but these are payload fields, not event names. This is fine — they go in the `data` JSON column. Just noting for clarity.

**Finding 8.3 (Low): `session_root` vs `project_root` semantics could confuse workers.**

The design uses both `session_root` and `project_root` throughout. The TOML schema has `dag.session_root` and `dag.project_root`. For most use cases they're the same (`.`). Workers might conflate them. The T0 task spec should explicitly test the case where they differ.

---

## SEVERITY SUMMARY

| Severity | Count | Findings |
|----------|-------|----------|
| Critical | 1 | 2.1 (orphan panes on governor death) |
| High | 3 | 2.2 (no deferred merge command), 3.1 (T2 too large), 5.1 (merge state sync) |
| Medium | 5 | 2.3 (skip propagation), 2.4 (prompt augmentation), 3.2 (T1 refactor risk), 5.2 (non-conflict errors), 8.1 (hash algorithm) |
| Low | 8 | 1.1, 1.2, 2.5, 3.3, 4.2, 5.3, 5.4, 6.2, 7.2, 8.2, 8.3 |

---

## GO/NO-GO VERDICT

**CONDITIONAL GO** — The design is fundamentally sound. The API contracts are correct, the TOML choice is justified, the merge ordering is safe, and the integration plan is clean. However, three issues must be addressed before implementation:

1. **Split T2** into two tasks (execution loop vs. multi-tier orchestration). A single hunter task will stall on the current scope.
2. **Specify orphan pane reconciliation** on resume. The current design has a governor-death race condition.
3. **Add deferred merge support** (either a `dgov dag merge` command or documented resume behavior for `--no-auto-merge`).

With those fixes, this is ready to dispatch.
