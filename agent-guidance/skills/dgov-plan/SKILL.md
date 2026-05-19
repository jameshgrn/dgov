---
name: dgov-plan
description: |
  Plan operations — the primary dispatch surface for implementation work.
  Use plan trees for multi-step work; plans compile to DAGs and execute
  through dispatch, review, merge, and evaluation.
author: Jake Gearon
version: 7.1.0
date: 2026-05-19
---

# dgov-plan — Primary Dispatch Surface

Use plans when work needs explicit file claims, dependencies, repeatable
verification, or multiple tasks. Use `dgov fix` only for narrow one-task work.

## Plan Tree Workflow

1. Create a directory: `.dgov/plans/<name>/`
2. Write `_root.toml` with a `[plan]` section.
3. Write task files in section directories, commonly `tasks/*.toml`.
4. Compile structurally before dispatch:
   `uv run dgov compile .dgov/plans/<name> --dry-run`
5. Compile dispatch-ready output:
   `uv run dgov compile .dgov/plans/<name>`
6. Validate the compiled plan:
   `uv run dgov validate .dgov/plans/<name>`
7. Run:
   `uv run dgov run .dgov/plans/<name>`
8. Monitor:
   `uv run dgov watch`
9. Inspect:
   `uv run dgov plan status .dgov/plans/<name>`
   `uv run dgov plan review .dgov/plans/<name>`

Example structure:

```text
.dgov/plans/feature-x/
├── _root.toml
├── _compiled.toml
└── tasks/
    ├── implement.toml
    └── test.toml
```

## Root File

```toml
[plan]
name = "feature-x"
summary = "Add feature X with focused tests."
sections = ["tasks"]
# default_agent = "provider/model-name"  # optional
```

## Task File

```toml
[tasks.implement]
summary = "Add feature X to module.py"
prompt = """
Orient:
- Read `src/module.py` and `tests/test_module.py`.
- This task only changes feature X behavior.

Edit:
1. Update `src/module.py` to handle the new case.
2. Add focused tests in `tests/test_module.py`.

Verify:
- `uv run pytest -q -m unit tests/test_module.py`
- `uv run ruff check src/module.py tests/test_module.py`
"""
commit_message = "Add feature X"
role = "worker"
files = { edit = ["src/module.py", "tests/test_module.py"] }
test_cmd = "uv run pytest -q -m unit tests/test_module.py"
```

## Structured File Claims

```toml
[tasks.example.files]
create = ["src/new.py"]
edit = ["src/existing.py"]
delete = ["src/old.py"]
read = ["src/reference.py"]
touch = ["src/maybe_create_or_edit.py"]
```

Prefer explicit `create`, `edit`, `delete`, and `read` over `touch`. Flat
shorthand such as `files = ["a.py"]` is valid, but it hides whether the file
already exists.

## Roles

| Role | Writes code | Use for |
|------|-------------|---------|
| `worker` | Yes | Implementation and tests |
| `researcher` | No | Bounded read-only analysis |
| `reviewer` | No | Reviewing dependency diffs |

Reviewer tasks can rely on auto-generated dependency diffs:

```toml
[tasks.review]
summary = "Review implementation and tests"
role = "reviewer"
depends_on = ["implement", "test"]
```

If reviewer verification should be bounded to landed diffs and prior checks,
say that explicitly in the prompt.

## Commands

| Command | Purpose |
|---------|---------|
| `uv run dgov init-plan <name>` | Scaffold a plan tree |
| `uv run dgov plan create "<goal>"` | Ask the planner agent to draft a plan |
| `uv run dgov compile <dir> --dry-run` | Structural compile without SOP reassignment side effects |
| `uv run dgov compile <dir>` | Compile current source to `_compiled.toml` |
| `uv run dgov validate <dir>` | Validate the compiled plan without running |
| `uv run dgov run <dir>` | Compile and execute a plan directory |
| `uv run dgov run --continue <dir>` | Continue from prior state and retry failed tasks |
| `uv run dgov run --restart <dir>` | Clear prior state and rerun from scratch |
| `uv run dgov plan list` | List plans with deploy progress (`--all`, `--archived`) |
| `uv run dgov plan status <dir>` | Show pending vs deployed units |
| `uv run dgov plan review <dir>` | Post-hoc debrief of the last run |
| `uv run dgov watch` | Live event stream |

## Key Principles

1. File claims are the source of truth for scheduling and settlement.
2. Dependencies express real ordering constraints only.
3. Prompts must use Orient / Edit / Verify structure.
4. Verification commands must be exact and copy-pasteable.
5. Repeated verification commands belong in `[verify.<name>]` recipes.
6. Repo-wide guidance belongs in `.dgov/governor.md` or `.dgov/sops/*.md`,
   not in one-off task prompts.
