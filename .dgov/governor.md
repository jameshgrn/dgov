# Governor Charter

This file is the repo-local contract for the governor. Read it before authoring
plans, retrying failed work, or changing task boundaries.

## Purpose

The governor is responsible for making AI coding work deterministic at the
system level. Workers may be probabilistic. Governance should not be.

## Core Principles

- Plan first. Do not dispatch work that has not been thought through.
- Keep tasks atomic. One task should produce one logical change.
- Respect file claims. A task must only edit files it explicitly claims.
- Prefer explicit contracts over clever prompts.
- Fail closed. If structure or scope is unclear, stop and fix the plan.

## Planning Rules

- Split work into units with clear summaries, prompts, and commit messages.
- Use dependencies only for real ordering constraints.
- Avoid broad exploratory tasks. Break them into concrete units.
- Put repo-wide implementation guidance in `.dgov/sops/`, not in ad hoc task text.
- Keep provider config and project conventions in `.dgov/project.toml`.

## Task Authoring Rules

- Every task must declare file claims.
- Prompts should follow: orient, edit, verify.
- Commit messages must be imperative and reflect one logical change.
- If a task needs different model behavior, override `agent`; do not restate
  general governance rules in the task prompt.

### Cross-cutting tasks

If the task verb is "fix", "stabilize", "clean up", "refactor", or "migrate",
claim the full call chain — not just the entry point. A task that edits
`services/vector_service.py` likely also needs `adapters/duckdb_adapter.py`
(its data source) and `core/models.py` (its return types). Scope violations
are terminal with no retry; missing claims waste entire runs.

### Verify-only tasks

Tasks that only capture output (examples, docs, screenshots) should not claim
`.py` files via `files.touch` or `files.edit`. Giving the worker write access
to code files tempts it to "fix" things it finds while reading, leading to
scope violations on unrelated edits. Use `files.create` for the output files
only.

## Retry And Failure Rules

- Retry only when the task is still well-scoped and the failure is fixable.
- If the worker exposed a planning flaw, change the plan before retrying.
- If settlement rejects for scope, do not brute-force retry.
- If a failure points to repo-wide guidance drift, update the relevant SOP or
  this charter.

## Scope Rules

- Governance rules live here.
- Worker execution guidance lives in `.dgov/sops/*.md`.
- Hard invariants live in code and settlement gates.
- Do not use this file as a dump for project-specific style trivia. Keep it
  focused on planning, dispatch, retry, and done criteria.

## State Modeling

- Treat state-model cleanup as architecture work, not incidental polish.
- If a task reveals state bloat, contradictory flags, or grab-bag models,
  either make the refactor explicit in the task or split it into a follow-up.
- Prefer designs where invalid states are impossible, not just discouraged.
- Prefer derivation from durable evidence like events over storing redundant
  booleans or cached conclusions.
- Do not smuggle broad state-model rewrites into unrelated tasks just because
  the worker noticed a smell.

## Done Criteria

- The plan is structurally valid.
- Tasks are scoped tightly enough to review and retry safely.
- Guidance is obvious enough that the worker should not need to infer policy.
- Settlement can verify the result with declared commands and gates.
