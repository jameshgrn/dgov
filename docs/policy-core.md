# Policy Core Architecture

This document describes the core architectural policies that govern dgov's execution model, quality gates, and recovery mechanisms. These are not optional style preferences — they are architecture constraints that ensure reliability, auditability, and bounded risk in AI-driven code changes.

## Event-Driven Monitor

The monitor daemon (`src/dgov/monitor.py`) replaced the polling-based `PostDispatchKernel` pattern with an **event-driven state machine** that advances worker panes based on persisted events rather than time-based polling loops.

### Before: Polling-Based Kernel

The old `PostDispatchKernel` used tier barriers, `time.sleep()` loops, and governor polling to advance workers through states. This approach had several problems:

- **CPU spin**: Continuous polling wasted CPU cycles between checks
- **Latency**: State transitions were delayed by poll intervals
- **Race conditions**: TOCTOU bugs when checking pane state vs. reading logs
- **Complexity**: Tier barriers and sleep loops made the control flow hard to reason about

### After: Event-Driven Progression

The new monitor uses a **named pipe notification system** (`events.pipe`) for all wait operations. The kernel emits events on every state transition, and the monitor blocks on `select()` until an event arrives.

**Key design principles:**

1. **Every state transition emits an event.** No silent state changes anywhere. Events are the audit trail AND the notification mechanism (pipe wakeup). If it didn't emit, it didn't happen.

2. **Event journal is source of truth.** The monitor bootstraps its candidate sets from the event journal (`read_events()`) rather than re-scanning pane records. This ensures consistency between what happened and what the monitor acts on.

3. **Deterministic classification first.** Output classification uses three layers:
   - **Layer 0**: Monitor hooks (user-configured TOML overrides)
   - **Layer 1**: Deterministic regex patterns (fast, no API call)
   - **Layer 2**: LLM classification for ambiguous cases (fallback only)

4. **Bounded retry with role escalation.** The monitor enforces automatic retry policies:
   - 2 attempts per tier (worker → supervisor → manager)
   - 3 tiers maximum before governor alert
   - Max 6 attempts before human intervention
   - Each retry gets specific failure context from the event log

5. **No `time.sleep()` in orchestration.** Named pipe notification for all wait operations. `select()` blocks on kernel event, never CPU spin. Acceptable sleeps: tmux sequencing delays, UI refresh loops, SQLite lock backoff. Nothing else.

### Monitor Hooks

Monitor hooks are configured via TOML files (`~/.dgov/monitor-hooks.toml` and `<project>/.dgov/monitor-hooks.toml`). They allow users to override default classification behavior with custom regex patterns:

```toml
[[hooks.hook]]
pattern = "all tests passing"
kind = "done"
message = "Tests passed, ready to merge"

[[hooks.hook]]
pattern = "waiting for user input"
kind = "nudge"
message = "You're waiting for input. Consider using --interactive flag."
keystroke = "?"
```

Hooks provide deterministic control over monitor behavior without modifying source code.

## Quality Gates: Tests and Lint Before Model Review

The review pipeline enforces **deterministic quality gates before invoking expensive LLM judgment**. This follows the intelligence hierarchy: determinism → statistics → LLM.

### Review Pipeline Stages

1. **Stage 1: Deterministic Inspection** (always runs, free)
   - Check test existence and pass status
   - Run ruff lint on changed Python files
   - Verify file claims match actual touched files
   - Detect protected file modifications
   - Compute commit count and diff stats

2. **Stage 2: Model Review** (only if Stage 1 passes)
   - Invoked only when deterministic gates pass
   - Requires explicit `review_agent` configuration
   - Can downgrade verdict from "safe" to "review"
   - Cannot upgrade a failed deterministic check

### Enforcement Points

The `run_review_only()` function in `src/dgov/executor.py` implements this two-stage gate:

```python
def run_review_only(
    project_root: str,
    slug: str,
    *,
    tests_pass: bool = True,
    lint_clean: bool = True,
    post_merge_check: str = "",
) -> ReviewOnlyResult:
    """Run the canonical review operation without merging."""
    
    # Stage 1: Deterministic inspection (always runs, free)
    provider = get_provider(DecisionKind.REVIEW_OUTPUT, session_root=session_root)
    record = provider.review_output(request)
    
    # Stage 2: Model review (only if deterministic passed AND review_agent is set)
    if review_agent and record.decision.verdict == "safe" and record.decision.commit_count > 0:
        try:
            model_provider = ModelReviewProvider()
            model_record = model_provider.review_output(request)
            if model_record.decision.verdict != "safe":
                record = model_record  # Downgrade to review
        except ProviderError:
            logger.debug("Model review failed, using deterministic result")
```

### Why This Matters

- **Cost efficiency**: Don't pay for LLM calls when tests fail or lint errors exist
- **Speed**: Deterministic checks are instant; LLM review adds latency
- **Reliability**: Tests and lint are objective; LLM confidence scores are unreliable
- **Auditability**: Every review decision has a clear trace from deterministic evidence

## One Canonical Executor Pipeline

dgov must have exactly one policy owner for `preflight → dispatch → wait → review → merge → cleanup → recovery`. Governors and LT-GOVs should invoke that pipeline, not reimplement pieces of it in parallel entrypoints.

### Pipeline Functions

| Stage | Function | Module |
|-------|----------|--------|
| Preflight | `run_preflight()` | `preflight.py` |
| Dispatch | `run_dispatch_only()` | `executor.py` |
| Wait | `observe_worker()` | `monitor.py` |
| Review | `run_review_only()` | `executor.py` |
| Merge | `run_merge_only()` | `executor.py` |
| Cleanup | `run_cleanup_only()` | `executor.py` |
| Recovery | `maybe_auto_retry()` | `recovery.py` |

All CLI commands (`dgov pane land`, `dgov mission land`, etc.) route through these functions. There are no alternative paths that bypass preflight or skip review.

## File Claims as First-Class Control Plane

Declarative task file sets are the source of truth for scheduling, preflight, conflict checks, and targeted validation. Prompt-derived touches are a fallback for freeform pane prompts, not the preferred control plane.

### File Claim Verification

Before merging, dgov verifies that workers only touched declared files:

```python
# In merger.py:merge_worker_pane()
raw_claims = target.get("file_claims", "[]")
file_claims = json.loads(raw_claims) if isinstance(raw_claims, str) else (raw_claims or [])

if file_claims:
    actual_r = subprocess.run(["git", "-C", worktree_path, "diff", "--name-only", f"{base_sha}..HEAD"])
    actual_files = {f for f in actual_r.stdout.strip().splitlines() if f}
    claimed = set(file_claims)
    undeclared = actual_files - claimed
    
    # Filter out test files — tests are read context, not edit targets
    undeclared = {f for f in undeclared if not f.startswith("tests/")}
    
    if undeclared:
        violation_msg = f"Pane {slug} touched undeclared files: {sorted(undeclared)}"
        logger.warning("%s", violation_msg)
        emit_event(session_root, "claim_violation", slug, error=f"Undeclared files: {sorted(undeclared)}")
        if strict_claims:
            return {"error": violation_msg, "claim_violations": sorted(undeclared)}
```

File claims enable:
- **Precise test scope**: Only run tests for affected modules
- **Conflict detection**: Identify same-file overlap between parallel workers
- **Safety guarantees**: Workers can't silently modify unrelated files

## Wide Typed Columns Over JSON Blobs

All queryable data in SQLite must be typed columns — never JSON that requires `json_extract` to query. `WHERE verdict = 'safe'` must work, not `WHERE json_extract(data, '$.verdict') = 'safe'`.

### Schema Rules

**Acceptable TEXT blobs:**
1. Opaque archives not intended for SQL queries (raw transcripts, prompt text)
2. Variable-length lists serialized as JSON where the list itself is the value (`file_claims`, `stale_files`), never nested objects

**Required typed columns:**
- `state` (TEXT): Pane lifecycle state
- `verdict` (TEXT): Review verdict ("safe", "review", "failed")
- `commit_count` (INTEGER): Number of commits
- `tests_passed` (BOOLEAN): Test pass status
- `lint_clean` (BOOLEAN): Lint pass status
- `eval_id` (TEXT): Stable eval identifier attached to a DAG run
- `kind` (TEXT): Eval kind (`regression`, `edge`, `invariant`, etc.)
- `unit_slug` (TEXT): Unit-to-eval link for executable coverage

This enables efficient queries like:
```sql
SELECT slug FROM panes WHERE state = 'done' AND verdict = 'safe' AND commit_count > 0;
```

Without typed columns, you'd need:
```sql
SELECT slug FROM panes 
WHERE json_extract(metadata, '$.state') = 'done'
  AND json_extract(review_data, '$.verdict') = 'safe';
```

Which is slow, error-prone, and harder to maintain.

### Plan Contracts Must Persist as Typed Rows

Plan evals are not documentation fluff. They are part of the executable
contract, so they must persist in typed tables keyed by `dag_run_id`.

- `dag_evals` stores the eval contract itself (`eval_id`, `kind`, `statement`, `evidence`).
- `dag_unit_eval_links` stores which units claim to satisfy which evals.
- `definition_json` may archive the same information, but review/reporting code must not rely on reparsing it.

This enables queries like:

```sql
SELECT eval_id, kind
FROM dag_evals
WHERE dag_run_id = 17;
```

and

```sql
SELECT unit_slug, eval_id
FROM dag_unit_eval_links
WHERE dag_run_id = 17
ORDER BY unit_slug, eval_id;
```

## Intelligence Hierarchy: Determinism → Statistics → LLM

Use the cheapest sufficient signal. Never use LLM confidence scores as escalation signals — models are overconfident when wrong.

### Escalation Triggers

Escalation happens on:
1. **Consensus**: Two cheap providers disagree (not confidence)
2. **Validation**: Output fails property tests (lint, tests, file claims)
3. **Calibration**: Historical accuracy is low (from decision journal)

Disagreement is a real signal; confidence is vibes.

### Application in Review Pipeline

- **Deterministic checks first**: Tests, lint, file claims, diff structure — all checked without an LLM
- **Statistical second**: Reliability scores, latency, retry rates from the decision journal
- **LLM last**: Judgment only when the first two are insufficient

## Zero Tolerance for Policy Violations

Every rule in this Policy Core section is an architecture constraint, not a style preference. If you find code that violates a rule, fix it immediately — do not ship the violation and plan to fix it later. If a rule conflicts with a task requirement, raise it before writing code. No exceptions, no "we'll clean it up next sprint."

---

## Related Documentation

- [Architecture](architecture.md) — High-level module map and data flow
- [State and Events](state-and-events.md) — Event journal and state machine details
- [Pane Lifecycle](pane-lifecycle.md) — Worker pane states and transitions
- [Configuration Files](configuration-files.md) — Agents.toml and monitor hooks
- [Review Fix Pipeline](review-fix-pipeline.md) — Automated code review and fix process
