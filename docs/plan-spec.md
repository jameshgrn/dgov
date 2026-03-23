# Plan Specification

A plan is a structured contract between the governor's high-level planning and the mechanical execution of tasks via a Directed Acyclic Graph (DAG).

## Format

Plans are written in TOML format. A plan consists of a global `[plan]` section and one or more `[units.<slug>]` sections.

```toml
[plan]
version = 1
name = "my-feature-plan"
goal = "Implement a new feature with tests"
default_agent = "worker"
max_retries = 2

[units.add-logic]
summary = "Implement the core logic"
prompt = "Read src/logic.py and implement X..."
commit_message = "feat: add core logic"
files = { edit = ["src/logic.py"] }

[units.add-tests]
summary = "Add unit tests"
depends_on = ["add-logic"]
prompt = "Write tests for src/logic.py in tests/test_logic.py"
commit_message = "test: add logic tests"
files = { create = ["tests/test_logic.py"] }
```

## LT-GOV Integration

You can dispatch a Lieutenant Governor (LT-GOV) as a plan unit. An LT-GOV is a sub-governor that can in turn dispatch its own workers to complete a complex task.

To define an LT-GOV task, set `role = "lt-gov"` and use the `lt-gov` template. You must provide a `task_list` in the `[units.<slug>.vars]` section.

```toml
[units.complex-refactor]
summary = "Orchestrate a complex multi-file refactor"
role = "lt-gov"
template = "lt-gov"
commit_message = "refactor: complex overhaul (via LT-GOV)"
files = { edit = ["src/api.py", "src/models.py"] } # Total scope for locking

[units.complex-refactor.vars]
task_list = """
1. Read src/api.py and identify bottlenecks.
2. Dispatch a worker to move validation to src/validators.py.
3. Dispatch a worker to update src/models.py to use new validators.
4. Verify all tests pass.
"""
```

### LT-GOV Variables

The `lt-gov` template automatically receives the following variables during plan compilation if not explicitly provided:

- `ltgov_slug`: Defaults to the unit's slug.
- `default_agent`: Defaults to the plan's `default_agent`.
- `task_list`: The structured instructions for the LT-GOV to follow.

## Acceptance Criteria

Each unit can define deterministic quality gates that must pass before the task is considered complete and ready for merge.

```toml
[units.my-task.acceptance]
tests_pass = true   # Run related tests (default: true)
lint_clean = true   # Run ruff check (default: true)
custom_check = "grep -q 'done' output.txt" # Custom shell command
```

## File Scopes

File scopes are used for:
1. **DAG Scheduling**: Units with overlapping files are serialized; disjoint units run in parallel.
2. **Preflight Checks**: Verifying file existence and permissions before dispatch.
3. **Review Gating**: Ensuring only declared files were modified.

Possible keys: `create`, `edit`, `delete`, `read`.
