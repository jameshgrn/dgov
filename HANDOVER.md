# HANDOVER — 2026-03-22 (Plan system + cost pyramid + event-driven)

## Current State

283 tests passing across all touched files, 1 skipped (known same-file merge limitation). All on main, not pushed.

## Completed

### Plan System (Phase 6)
- `src/dgov/plan.py` — PlanSpec, PlanUnit, AcceptanceCriteria, PlanUnitFiles, PlanIssue
- `parse_plan_file` → `validate_plan` → `compile_plan` → `run_plan` (full pipeline)
- `serialize_plan` — TOML output for programmatic plan building
- `dgov plan validate/compile/run` CLI commands
- Config flow: permission_mode, max_retries, merge_resolve, review_agent all flow from PlanSpec → DagDefinition → DagTaskSpec
- Version validation, file conflict detection, cycle detection, test existence gate
- 43 plan tests

### Tiered Review (Cost Pyramid)
- `review_agent` field threaded through: PlanUnit → DagTaskSpec → ReviewTask → _dag_review → run_review_only
- `ModelReviewProvider` in decision_providers.py — sends diff to specified model via OpenRouter
- Two-stage sequential review: deterministic InspectionReview (free) → ModelReview (only if review_agent set AND deterministic passes)
- `_parse_review_response` / `_resolve_review_model` helpers
- 10+ review tier tests

### Role-Based Escalation
- `ROLE_ESCALATION`: worker → supervisor → manager → governor alert
- `_MODEL_TO_ROLE` mapping for backward compat with panes that stored model names
- Role aliases in `agents.toml`: `[routing.worker]`, `[routing.supervisor]`, `[routing.manager]`
- Escalation events: `quality_retry`, `quality_escalate`
- 5 role escalation tests

### Event-Driven Architecture (No Polling)
- Named pipe (`events.pipe`) replaces ALL `time.sleep` polling in orchestration
- `_ensure_notify_pipe` / `_notify_waiters` / `_wait_for_notify` in persistence.py
- `emit_event` writes byte to pipe after SQLite insert — instant cross-process wakeup
- `wait_for_events`, `wait_for_slugs`, `_dag_wait_any` all use `_wait_for_notify`
- Cross-platform (POSIX `select()` on named FIFO, works macOS + Linux)
- 7 notification tests (FIFO creation, timeout, notify-wakes-waiter, emit triggers)

### Merge Pipeline Fixes
- False-negative fix: `git merge --abort` + `reset --hard` after failed candidate merge
- Test-existence gate: deterministic check via `.test-manifest.json`, blocks merge
- Codex LT-GOV audit found 3 P0s, 4 P1s — all resolved
- Bulk `is_alive`, redundant cmd fetch, `_AGENT_COMMANDS` dedup, `_has_new_commits` guard

### Policy Core (7 new rules)
- No time.sleep in orchestration
- Roles not models
- Every state transition emits
- Quality gates deterministic first
- Bounded retry with role escalation
- Kernel never sleeps
- Plans are the contract

## Key Decisions
- **Ledger #72**: Plan schema + compiler separates governor cognition from execution
- **Ledger #76**: review_agent threaded through pipeline for tiered review
- **Ledger #77**: ModelReviewProvider cascade: deterministic first, model second
- **Ledger #79**: Four abstract tiers (worker/supervisor/manager/governor)
- Named pipe over kqueue — cross-platform, no platform-specific APIs

## Open Issues
- **Ledger #75**: river-35b (port 8080) process crashed remotely, not restarted
- **Ledger #80**: Audit all existing quality checks and unify into single gate pipeline
- **Ledger #81**: 4B model as smart-deterministic tier (formalize)
- **Ledger #83**: LT-GOV codex should produce PlanSpec, not dispatch workers directly
- Quality-gate retry in DagKernel not yet built (W3 from original plan — review failure triggers retry/escalation)
- AcceptanceCriteria defined but not enforced in post-merge pipeline (custom_check maps to post_merge_check but runner doesn't execute it)
- Monitor daemon still uses its own polling loop (separate from orchestration paths)

## Next Steps
1. **Build quality-gate retry in DagKernel** — when TaskReviewDone(passed=False), retry at same tier with failure context, escalate after 2 failures
2. **Wire AcceptanceCriteria into post-merge** — custom_check already maps to post_merge_check, need a runner
3. **Codex LT-GOV → PlanSpec output** — codex produces plans, governor executes
4. **Push to origin** — run full CI suite first
5. **Restart river-35b on remote** — process crashed, port 8080 dead

## Important Files

| File | What |
|------|------|
| `CLAUDE.md` | Policy Core (15 rules), role-based routing |
| `src/dgov/plan.py` | Plan schema, parse, validate, compile, serialize, run |
| `src/dgov/cli/plan_cmd.py` | dgov plan validate/compile/run |
| `src/dgov/decision_providers.py` | ModelReviewProvider, _parse_review_response |
| `src/dgov/provider_registry.py` | Review cascade wiring |
| `src/dgov/executor.py` | Two-stage review, _dag_review with review_agent, _wait_for_notify |
| `src/dgov/persistence.py` | Named pipe notification (_ensure_notify_pipe, _notify_waiters, _wait_for_notify) |
| `src/dgov/recovery.py` | ROLE_ESCALATION, _MODEL_TO_ROLE, quality events |
| `src/dgov/merger.py` | Candidate merge abort fix |
| `src/dgov/inspection.py` | check_test_coverage (deterministic gate) |
| `src/dgov/kernel.py` | DagKernel.review_agents, ReviewTask.review_agent |
| `src/dgov/dag_parser.py` | DagTaskSpec.review_agent |
| `src/dgov/done.py` | _AGENT_COMMANDS (single source), _has_new_commits guard |
| `src/dgov/status.py` | Imports _AGENT_COMMANDS from done.py, bulk is_alive |
| `~/.dgov/agents.toml` | Role routing aliases (worker/supervisor/manager) |
| `tests/test_plan.py` | 43 plan tests |
| `tests/test_decision_providers.py` | ModelReviewProvider + review_agent tests |
| `tests/test_persistence_pane.py` | Named pipe notification tests |
| `tests/test_bounded_retry.py` | Role escalation tests |
| `tests/test_kernel.py` | DagKernel review_agent tests |
