# Plan Specification

A plan is a structured contract between the governor's high-level planning and
the mechanical execution of tasks via a Directed Acyclic Graph (DAG).

Scratch plans should live under `.dgov/plans/`, not in the repo root. Create
one with `uv run dgov plan scratch <name>`, then edit the generated TOML before
running `validate`, `compile`, or `run`.

## Authoring guidance

- The plan is the contract. Start with evals, then derive units.
- Encode file claims in TOML instead of leaving scope implicit in prompts.
- Keep units small and concrete. One logical change per unit beats a vague omnibus task.
- Use `depends_on` only for real execution dependencies, not as a substitute for thinking through file claims.
- Prompts should tell the worker what to read, what to change, and what validation to run.
- Treat `.dgov/plans/*.toml` as scratch artifacts. Keep durable, reviewed plans somewhere intentional if they are part of repo history.

See also: [Eval-First Planning SOP](eval-first-planning.md)

## Format

Plans are written in TOML format. A plan consists of a global `[plan]` section,
one or more `[[evals]]` entries, and one or more `[units.<slug>]` sections.

```toml
[plan]
version = 1
name = "my-feature-plan"
goal = "Implement a new feature with tests"
max_retries = 2

[[evals]]
id = "E1"
kind = "regression"
statement = "The CLI creates scratch plans only under .dgov/plans/."
evidence = "uv run pytest tests/test_dgov_cli.py -q -m unit"
scope = ["src/dgov/cli/plan_cmd.py", "src/dgov/plan.py"]

[[evals]]
id = "E2"
kind = "invariant"
statement = "Scratch plan creation does not create repo-root TOML files."
evidence = "uv run pytest tests/test_plan.py -q -m unit"

[units.add-logic]
summary = "Implement the core logic"
prompt = "Read src/logic.py and implement X..."
commit_message = "feat: add core logic"
satisfies = ["E1"]
files = { edit = ["src/logic.py"] }

[units.add-tests]
summary = "Add unit tests"
depends_on = ["add-logic"]
prompt = "Write tests for src/logic.py in tests/test_logic.py"
commit_message = "test: add logic tests"
satisfies = ["E1", "E2"]
files = { create = ["tests/test_logic.py"] }
```

## Evals

`[[evals]]` entries are the primary planning artifact. Each eval should be:

- Falsifiable: it can clearly pass or fail.
- Observable: it names the evidence or command that will verify it.
- Scoped: use `scope` when the eval applies to specific files or subsystems.
- Useful: include regressions, edge cases, invariants, and explicit non-goals where needed.

Allowed `kind` values are:

- `regression`
- `happy_path`
- `edge`
- `invariant`
- `non_goal`
- `manual`
- `performance`
- `integration_test`
- `security`
- `scalability`
- `usability`
- `accessibility`
- `reliability`
- `maintainability`
- `testability`

Every unit must list the eval ids it satisfies with `satisfies = [...]`.
Plans without evals, units without `satisfies`, or orphaned evals fail
validation.

On submission, evals and unit-to-eval links are persisted as typed SQLite rows
for the DAG run. The TOML remains the authoring artifact; the database becomes
the queryable execution record.

## LT-GOV Integration

You can dispatch a Lieutenant Governor (LT-GOV) as a plan unit. An LT-GOV is a
sub-governor that can in turn dispatch its own workers to complete a complex
task.

To define an LT-GOV task, set `role = "lt-gov"` and use the `lt-gov` template.
You must provide a `task_list` in the `[units.<slug>.vars]` section.

```toml
[[evals]]
id = "E3"
kind = "manual"
statement = "The LT-GOV dispatches the intended worker set and reports progress."
evidence = "Review .dgov/progress/complex-refactor.json after completion."

[units.complex-refactor]
summary = "Orchestrate a complex multi-file refactor"
role = "lt-gov"
template = "lt-gov"
commit_message = "refactor: complex overhaul (via LT-GOV)"
satisfies = ["E3"]
files = { edit = ["src/api.py", "src/models.py"] }

[units.complex-refactor.vars]
task_list = """
1. Read src/api.py and identify bottlenecks.
2. Dispatch a worker to move validation to src/validators.py.
3. Dispatch a worker to update src/models.py to use new validators.
4. Verify all tests pass.
"""
```

### LT-GOV Variables

The `lt-gov` template automatically receives the following variables during plan
compilation if not explicitly provided:

- `ltgov_slug`: Defaults to the unit's slug.
- `default_agent`: Defaults to the plan's `default_agent`.
- `task_list`: The structured instructions for the LT-GOV to follow.

## Acceptance Criteria

Each unit can define deterministic quality gates that must pass before the task
is considered complete and ready for merge.

```toml
[units.my-task.acceptance]
tests_pass = true
lint_clean = true
custom_check = "grep -q 'done' output.txt"
```

Acceptance criteria are not a substitute for evals. Evals define what "done"
means at the plan level; acceptance gates are deterministic checks attached to a
specific unit.

## File Scopes

File scopes are used for:

1. DAG scheduling: Units with overlapping files are serialized; disjoint units run in parallel.
2. Preflight checks: Verifying file existence and permissions before dispatch.
3. Review gating: Ensuring only declared files were modified.

Possible keys: `create`, `edit`, `delete`, `read`.

## Scratch workflow

```bash
uv run dgov plan scratch review-refactor
$EDITOR .dgov/plans/review-refactor.toml
uv run dgov plan validate .dgov/plans/review-refactor.toml
uv run dgov plan compile .dgov/plans/review-refactor.toml
uv run dgov plan run .dgov/plans/review-refactor.toml
```
