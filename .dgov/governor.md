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
- Distinguish running work from attention states in operator status.
  `dgov status` is `active` when workers or settlement are in flight,
  `needs_attention` when reviewed results await governor handling, and `idle`
  otherwise.
- Project-specific build, test, runtime, platform, data, CI, secrets, and
  convention policy lives in the target repo's `.dgov/project.toml`,
  `.dgov/sops/`, or repo scripts. Core dgov owns generic hooks, diagnostics,
  and scope enforcement; it does not hardcode target-project wrappers. See
  `.dgov/sops/project-extensions.md`.
- In this source repo, `.dgov/governor.md` and `.dgov/sops/` are the canonical
  bootstrap policy sources. Built distributions derive
  `dgov.bootstrap_policy_data` from those files; do not hand-maintain Markdown
  mirrors under `src/dgov/bootstrap_policy_data/`.
- dgov-owned machine-agent skills are canonical under `agent-guidance/skills/`.
  Built distributions derive `dgov.agent_skill_data` from those files; do not
  hand-maintain skill mirrors under `src/dgov/agent_skill_data/`. Local
  `~/.agents/skills/dgov-*` copies are derived machine state refreshed with
  `uv run dgov agents sync`.
- Commit durable `.dgov/` source before dispatch. Worker deployment and
  post-run sentrux finalization are separate states; uncommitted governance
  source blocks dispatch even when generated runtime artifacts are dirty.
  Plan `_root.toml` and task TOML files are durable source;
  `_compiled.toml`, `.dgov/plans/deployed.jsonl`, `.dgov/runs.log`,
  `.dgov/state.db*`, `.dgov/out/`, and `.dgov/runtime/` are generated/runtime
  state.

## Governor Invocation

When operator direction is ambiguous, classify the work before dispatching.
Classification is the governor's job; workers should not infer it from
scattered policy.

### Intent classes

| Class | Distinguishing question | Next action |
|-------|------------------------|-------------|
| Implementation | Does the operator want product or code behavior to change? | Write a normal plan with explicit file claims and targeted verification. |
| Governance repair | Are this repo's `.dgov/` files, plan state, or core dgov code wrong? | Plan against `.dgov/` or `src/dgov/`. |
| Project policy | Is the blocker in a target repo's `.dgov/project.toml`, `.dgov/sops/`, or repo scripts? | Update that project's policy surfaces; do not change core dgov. |
| Durable memory | Should a learned rule, bug, decision, or debt outlive this session? | Record it in the ledger; if operational, promote it into this charter or an SOP. |

When implementation and governance-repair both apply, do governance-repair
first; the implementation plan inherits the corrected state.

If context risk is becoming the primary failure mode, write or refresh the
handover before dispatching more work. Do not invent new implementation scope
to fill a stale session.

### Failure-to-task catalog

Each entry maps observable evidence to a typed next task. Use these when a
run ends in a known failure shape. Run `dgov diagnose` to evaluate the
mechanical entries against live repo state; entries marked as having no
mechanical signal must be checked by hand.

**agent_skill_drift**
- Evidence: A dgov skill under `~/.agents/skills/dgov-*` references removed
  CLI commands, contradicts `.dgov/governor.md`, or differs from the shipped
  source bundle.
- Class: Governance repair.
- Next action: Update `agent-guidance/skills/`, verify package force-include
  and drift checks, then run `uv run dgov agents sync` on the machine that
  needs the refreshed skills.
- Do not: Edit local `~/.agents/skills/dgov-*` as the only fix.

**archive_policy_drift**
- Evidence: `git check-ignore .dgov/plans/archive/<plan>/_root.toml` matches
  a `.gitignore` rule.
- Class: Project policy (target repo).
- Next action: Edit the target repo's `.dgov/.gitignore` so durable plan
  archives are trackable; retry the finalization path.
- Do not: Rerun the landed worker task to recover bookkeeping. Worker-task
  completion and governor finalization are separate states.

**cli_internal_helper_import**
- Evidence: A CLI module imports a sibling underscore-prefixed helper from another `dgov.cli.*` module; the compose path bypasses the public API surface.
- Class: Implementation.
- Next action: Compose through `dgov.cli.compile.compile_plan_dir`, `dgov.cli.run.run_compiled_plan`, `dgov.cli.run.run_sentrux`/`sentrux_available`; add a boundary test rejecting underscore cross-module imports.
- Do not: Add underscore-prefixed imports to fix a circular — restructure the public surface instead.

**cross_file_duplicate_false_charge**
- Evidence: Settlement charges a task for duplicate symbols that already existed in target `HEAD` pre-merge.
- Class: Implementation.
- Next action: Compare candidate duplicate groups against both task base and target `HEAD` before charging; pre-existing duplicates do not belong to the task.
- Do not: Resolve duplicates by renaming task-introduced symbols — fix the comparison.

**guidance_drift**
- Evidence: A failure points at advice that is missing, contradictory, or
  out of date in `.dgov/governor.md` or `.dgov/sops/`.
- Class: Governance repair.
- Next action: Update the SOP or charter section first; then dispatch the
  implementation work.
- Do not: Repeat the guidance inside a single task prompt.

**headless_interpreter_mismatch**
- Evidence: Headless worker fails outside the source tree because it ran via the target-repo `uv run` instead of the installed dgov interpreter.
- Class: Implementation.
- Next action: Launch headless workers with the installed dgov interpreter; bootstrap missing governor/SOPs from package assets.
- Do not: Require every target repo to vendor dgov dependencies.

**plan_claims_violation**
- Evidence: Settlement rejects for scope; the worker edited a file outside
  its claims.
- Class: Governance repair.
- Next action: Fix the plan's file claims or decompose the task; re-run.
- Do not: Brute-force retry the same plan. Scope violations are terminal.

**post_run_sentrux_refresh_metadata_conflict**
- Evidence: A full-plan run or `dgov.dispatch` lands worker commits, then exits
  nonzero during accepted sentrux baseline refresh with only dgov-generated
  metadata dirty, such as `.dgov/plans/deployed.jsonl` or plan `_compiled.toml`.
- Class: Governance repair.
- Next action: Treat the worker deployment and post-run baseline refresh as
  separate states. The canonical fix is in core dgov: refresh accepted sentrux
  baseline from a clean detached `HEAD`, copy back only `.sentrux` baseline
  outputs, and tolerate known dgov run metadata while still rejecting source
  dirt. In target repos pinned to older dgov, commit the dgov metadata before
  retrying the refresh path.
- Do not: Mark the landed worker task as failed, ignore all nonzero dispatch
  exit codes, allow arbitrary `.dgov/` dirt, or disable sentrux.

**prompt_budget_overflow**
- Evidence: Worker or researcher prompt blows budget on unbounded tree snapshots or tool output.
- Class: Implementation.
- Next action: Enforce runtime prompt-budget caps on snapshots and tool output independent of config view limits.
- Do not: Raise the budget without a cap — the unbounded surface is the bug.

**reviewer_scope_unbounded**
- Evidence: A reviewer task improvises broad `run_tests()` or shell verification, widening scope past the landed diffs it is supposed to evaluate.
- Class: Governance repair.
- Next action: Reviewer prompts must declare a bounded verification surface; forbid generic verifies; rely on commit-derived changed files as the source of truth.
- Do not: Treat the reviewer as a second-pass worker — review is read-only against landed diffs.

**run_bash_verify_misuse**
- Evidence: A worker invokes `ruff`, `pytest`, or `ty` via raw `run_bash`, bypassing settlement-equivalent gates; or `uv run` resolves against ambient `PATH` and fails inside the sandbox.
- Class: Implementation.
- Next action: Classify wrapped verify commands in the `run_bash` parser; route them to `lint_check`/`run_tests`/`type_check`/`format_file`; ensure sandbox `PATH` resolves `uv` directly.
- Do not: Add a one-off allowlist for the failing command — fix the parser.

**runtime_fix_plan_lifecycle**
- Evidence: `dgov clean` leaves or deletes runtime fix-plans inconsistently, or manual post-hoc documentation lands in the plan/archive lane instead of the ledger.
- Class: Project policy.
- Next action: `dgov clean` preserves unresolved entries under `.dgov/runtime/fix-plans/` and archives only resolved entries; route post-hoc documentation to `dgov ledger`.
- Do not: Use the plan/archive lane as a durable documentation surface.

**scope_ignore_reserved_path**
- Evidence: A `[scope] ignore_files` entry covers a reserved governance path (for example `.sentrux/baseline.json` or `.dgov/plans/deployed.jsonl`) and project config loads without rejection, but settlement still surfaces a scope error against that path.
- Class: Governance repair.
- Next action: Validate `[scope] ignore_files` entries against settlement's effective matching semantics with reserved-path samples at config load; fail fast on any entry that masks a reserved path.
- Do not: Add ad-hoc exceptions in settlement to "ignore the ignore" — fix the validator.

**self_hosting_settlement_snapshot**
- Evidence: Settlement gates pass or fail against the live runner process while editing governor-owned code; the result is judged against stale imports.
- Class: Governance repair.
- Next action: Execute settlement gates from the candidate snapshot in a subprocess; never import governor-owned modules from the running runner for gating decisions.
- Do not: Reload modules in-process to "refresh" — subprocess isolation is the invariant.

**sentrux_baseline_drift**
- Evidence: Sentrux gate emits a stale-baseline warning (baseline is many
  commits behind HEAD or weeks old), or rejects with quality degradation
  whose offender list is entirely pre-existing functions not touched by
  the current diff.
- Class: Governance repair.
- Next action: Let a clean complete full-plan `dgov run` refresh accepted
  sentrux baseline metadata after the post-run comparison passes. Outside a
  full plan run, run `dgov sentrux gate-save` only if the drift is intentional
  and commit the refreshed `.sentrux/baseline.json` plus
  `.sentrux/dgov-baseline.json`.
- Do not: Game the import graph (dynamic imports, indirection) to satisfy
  the metric. Refactor real coupling when it exists, not when the baseline
  has drifted.

**stale_run_window**
- Evidence: `dgov plan review`, `dgov watch`, or scope checks return stale data because events persist across runs and reads are not scoped to the latest `run_start` for that plan.
- Class: Implementation.
- Next action: Lower-bound plan-scoped event reads on `max(id WHERE event='run_start' AND plan_name=X)`; pane-scope transient activity; rewrite events (not just `tasks`) on manual repair.
- Do not: Truncate events as a workaround — the run-window lower bound is the invariant.

**verify_recipe_missing**
- Evidence: The same toolchain command appears in multiple task prompts, or a
  worker fails because setup/lint/test invocation is ambiguous or repo-local.
- Class: Project policy.
- Next action: Add a `[verify.<name>]` recipe in `.dgov/project.toml` or a
  repo script; reference it by name from prompts.
- Do not: Add a language- or platform-specific wrapper to core dgov.

**worker_payload_drift**
- Evidence: A worker behaves inconsistently across headless launch, TOML fallback, and runtime extraction paths; fields like `type_check_cmd`, `line_length`, or LLM settings silently differ.
- Class: Implementation.
- Next action: Route all three payload paths through `dgov.workers.config`; add a contract test asserting a single payload shape across launch modes.
- Do not: Patch one path in isolation — the bug recurs when the next field is added.

**worktree_prep_drift**
- Evidence: A worker fails on import because shared `.venv`, `uv.lock`, or `__pycache__` residue leaked between worktrees; or a downstream task does not see its predecessor's outputs because the worktree was based off ambient `HEAD`.
- Class: Governance repair.
- Next action: Prepare pyproject worktrees per task; base dependent worktrees on the latest merged deploy sha; ensure default `scope_ignore_files` cover `.venv`, `uv.lock`, `__pycache__`, `*.pyc`.
- Do not: Reuse a stale worktree across tasks, or trust ambient `HEAD` for dependent tasks.

### Updating this catalog

When a ledger entry of class `rule` or `pattern` describes a recurring failure
shape, add or revise the matching catalog entry in the same change. The catalog
is the governor-facing index into durable memory; if the ledger learns
something this section does not, the index has drifted.

## Planning Rules

- Split work into units with clear summaries, prompts, and commit messages.
- Use dependencies only for real ordering constraints.
- Avoid broad exploratory tasks. Break them into concrete units.
- Put repo-wide implementation guidance in `.dgov/sops/`, not in ad hoc task text.
- Keep provider config and project conventions in `.dgov/project.toml`.
- When verification commands repeat across plans, define them as `[verify.<name>]`
  recipes in `.dgov/project.toml` and reference them by name instead of embedding
  full commands in every task prompt.

## Operational Memory

- The ledger is the durable memory for bugs, rules, decisions, patterns, and debt.
- `HANDOVER.md` is a session snapshot for current state and next steps, not a
  source of truth.
- `.napkin.md` is local scratch for workflow quirks and provisional notes.
- If a note in handover or napkin is durable, promote it into the ledger or the
  relevant charter/SOP instead of duplicating it indefinitely.
- After structural/Sentrux cleanup, rerun `uv run dgov sentrux offenders`,
  resolve stale offender debt, and add the current offender snapshot if debt
  remains.

## Preflight Discipline

- Two definitions of "green" must not diverge. File-scoped local checks
  (`uv run ruff check <file>`, `uv run ty check <file>`) are not equivalent
  to the full settlement gate. Before committing a checkpoint that will feed
  `dgov run`, run the full gate set: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run ty check`,
  `uv run pytest tests/test_boundaries.py`, and the unit marker sweep
  (ledger #34).
- `tests/test_boundaries.py` enforces architectural import boundaries
  (worker → orchestration, settlement → persistence, kernel → I/O). It runs
  in under 100 ms; there is no cost reason to skip it on commits touching
  `src/` (ledger #36).
- `dgov preflight` wraps the settlement gate equivalent against the working
  tree without dispatching a worker. Prefer it over running each tool
  individually.

## Plan Authoring Workflow

This is the sequence for going from idea to running plan.

1. **Define the goal.** One sentence. What does this plan accomplish?
2. **Audit the change set.** Read the code. Trace imports and call chains.
   Identify every file that needs to change. For cross-cutting work, follow
   dependencies outward — entry point, data sources, return types, tests.
3. **Decompose into tasks.** One task = one logical commit. Group by section
   in the plan tree (e.g. `scaffold/`, `core/`, `tests/`, `docs/`).
4. **Assign file claims.** Use explicit `create`/`edit`/`read`/`delete`
   semantics. Cross-check every path mentioned in the prompt against the
   claims. This is where plans fail — under-specified claims cause scope
   violations, which are terminal with no retry.
5. **Write prompts.** Follow Orient / Edit / Verify structure (see below).
   Numbered steps, specific file paths, exact verify commands.
6. **Set dependencies.** Only where real ordering exists: B reads a file A
   creates, or B edits a file A also edits. If two tasks touch different
   files, let them run in parallel.
7. **Compile and validate.** `dgov compile <dir> --dry-run`. Fix every
   error before running. Review warnings by signal. Warnings about unclaimed
   prompt references are almost always real bugs; prompt-structure warnings
   are advisory unless they reveal a genuinely vague prompt. If you leave a
   warning in place, record why it is acceptable in the handover or plan notes.
8. **Run.** `dgov run <plan-dir>`. Watch in a second terminal with
   `dgov watch`.
9. **Diagnose failures.** Scope violation = plan bug (fix claims, re-run).
   Settlement failure = maybe retry, maybe plan bug. Empty diff = the worker
   didn't write changes (check prompt clarity).

## Task Authoring Rules

- Every task must declare file claims.
- Prompts must follow Orient / Edit / Verify structure.
- Commit messages must be imperative and reflect one logical change.
- If a task needs different model behavior, override `agent`; do not restate
  general governance rules in the task prompt.
- Use `self_review = true` on tasks where the worker is likely to make
  semantic mistakes (e.g. wrong method receiver, unused return values,
  incorrect API usage). Self-review spawns a clean-context reviewer on
  the diff after the worker finishes. If the reviewer rejects, the worker
  gets one fix attempt, then auto-passes to settlement regardless.
- Use `max_fork_depth` (default 1) to control how many times a worker
  that exhausts its iteration budget is relaunched with a clean context.
  Each fork resets the iteration counter but preserves the worktree state.
  Set to 0 to disable forking; set higher (e.g. 2–3) for large tasks
  that legitimately need multiple passes.

### Prompt structure

Every prompt should have three phases. Label them explicitly.

**Orient:** What to read first. What context matters. What the task must NOT
do. Constraints and boundaries. Tell the worker to read specific files before
editing anything.

**Edit:** Numbered steps. Specific file paths. Specific changes. Prefer
`edit_file` over `write_file` for existing files. If creating multiple related
files, describe each one and its purpose.

**Verify:** Exact commands the worker should run. Not "run tests" but
`uv run pytest -q -m unit tests/test_foo.py`. Not "check lint" but
`uv run ruff check src/module/file.py`. The worker should be able to
copy-paste these.

### File claim semantics

Use explicit intent over ambiguous shorthand.

| Field | Meaning | Scope effect |
|-------|---------|--------------|
| `files.create` | New files this task brings into existence | Write access |
| `files.edit` | Existing files this task modifies | Write access |
| `files.read` | Files the task reads for context but must NOT modify | No write access; suppresses prompt cross-check warnings |
| `files.delete` | Files this task removes | Write access |
| `files.touch` | Shorthand "might create or edit" (flat `files = [...]` syntax) | Write access; governed like `create`/`edit`/`delete` for department ownership checks (ledger #67) |

**Prefer explicit `create`/`edit`/`read` over `touch`.** The flat shorthand
(`files = ["a.py", "b.py"]`) is valid but ambiguous — you cannot tell at a
glance whether a file exists yet or is being created. Use it for quick one-off
plans; use explicit semantics for plans you need to debug or re-run.

**Rules:**
- If the prompt mentions a file path, it must appear in claims (`edit`,
  `create`, or `read`). The compiler warns on unclaimed prompt references.
- If changing a public interface (type signature, function signature, enum
  value), claim the corresponding test files. Workers will correctly try to
  fix failing tests, but scope violation fires if those files are not claimed.
- Do not give workers write access to files they do not need to change.
  Extra access tempts "while I'm here" edits that trigger scope violations.
- `files.read` is the right mechanism for read-only context. Workers can read
  without triggering scope violations — only writes are gated.

### Cross-cutting tasks

If the task verb is "fix", "stabilize", "clean up", "refactor", or "migrate",
claim the full call chain — not just the entry point. A task that edits
`services/vector_service.py` likely also needs `adapters/duckdb_adapter.py`
(its data source) and `core/models.py` (its return types).

To discover the call chain: read the entry-point file, follow its imports,
check what types it returns and where those types are defined, and check which
test files import from the modules you are changing.

Scope violations are terminal with no retry. Missing claims waste entire runs.

### Verify-only tasks

Tasks that only capture output (examples, docs, screenshots) should not claim
`.py` files via `files.touch` or `files.edit`. Giving the worker write access
to code files tempts it to "fix" things it finds while reading, leading to
scope violations on unrelated edits. Use `files.create` for the output files
only, and `files.read` for any source files the task needs to inspect.

### Task fields reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `summary` | string | required | One-line description |
| `prompt` | string | — | Inline instructions (Orient / Edit / Verify) |
| `prompt_file` | string | — | Path to external prompt file (mutually exclusive with `prompt`) |
| `commit_message` | string | — | Imperative commit message |
| `agent` | string | plan/project default | Model override for this task |
| `role` | string | `"worker"` | `"worker"`, `"researcher"`, or `"reviewer"` |
| `depends_on` | list | `[]` | Task slugs that must complete first |
| `files` | table/list | required | File claims (see above) |
| `timeout_s` | int | `900` | Per-attempt wall-clock timeout in seconds |
| `iteration_budget` | int | — | Max tool calls before exhaustion (overrides project default) |
| `test_cmd` | string | — | Task-specific test command for settlement |
| `sop_mapping` | list | `[]` | Optional source-task SOP pins; compiler also emits final assigned SOP names |
| `self_review` | bool | `false` | Spawn a clean-context reviewer on the diff after the worker finishes |
| `max_fork_depth` | int | `1` | Max clean-context relaunches when iteration budget is exhausted |

## Retry And Failure Rules

- Retry only when the task is still well-scoped and the failure is fixable.
- After a transient infrastructure failure, use `dgov run --continue <plan-dir>`
  to retry failed or abandoned tasks without restarting already merged work.
- If the worker exposed a planning flaw, change the plan before retrying.
- If settlement rejects for scope, do not brute-force retry. Scope violations
  are terminal — the review gate is before settlement and is not retryable.
  Fix the file claims and re-run.
- If a failure points to repo-wide guidance drift, update the relevant SOP or
  this charter.
- Empty diffs at done mean the worker did not write changes. Check prompt
  clarity — the Orient/Edit/Verify structure was probably missing or vague.

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

- The plan is structurally valid (`dgov compile --dry-run` passes with no
  errors).
- Tasks are scoped tightly enough to review and retry safely.
- Every prompt follows Orient / Edit / Verify with exact file paths and
  verify commands.
- File claims are explicit and complete — no unclaimed prompt references.
- Any remaining warnings have been reviewed and are understood, not ignored.
- Guidance is obvious enough that the worker should not need to infer policy.
- Settlement can verify the result with declared commands and gates.
