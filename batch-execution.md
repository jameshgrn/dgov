# Batch execution

The `batch.py` module provides checkpoint operations for saving and restoring repository state. Multi-step work is now handled through the **plan system** using `dgov plan run`.

## Plan-driven workflow

All implementation work goes through TOML plan files:

```toml
# .dgov/plans/multi-step.toml
name = "multi-step-refactor"
description = "Refactor multiple modules"

[[steps]]
name = "step-1-parser"
agent = "pi"
prompt = "Refactor parser.py error handling"
touches = ["src/parser.py"]

[[steps]]
name = "step-2-models"
agent = "pi"
prompt = "Update models to use new parser API"
touches = ["src/models.py"]
depends_on = ["step-1-parser"]

[[steps]]
name = "step-3-tests"
agent = "claude"
prompt = "Add tests for refactored code"
touches = ["tests/test_parser.py", "tests/test_models.py"]
depends_on = ["step-2-models"]
```

Run the entire plan:

```bash
dgov plan run .dgov/plans/multi-step.toml
```

The kernel compiles the plan into a DAG, handles parallelization of independent steps, and manages the full lifecycle (dispatch → wait → review → merge).

## Checkpoint operations

Use `batch.py` operations to save and restore repository state:

### Create a checkpoint

```bash
dgov batch checkpoint create my-checkpoint-name
```

Saves the current state of tracked files for later restoration.

### List checkpoints

```bash
dgov batch checkpoint list
```

Shows all saved checkpoints with timestamps.

### Restore a checkpoint

```bash
dgov batch checkpoint restore my-checkpoint-name
```

Restores the repository to the saved state.

## Migration from JSON batch specs

The old JSON batch spec format (used with `dgov batch spec.json`) has been replaced by plan files. Convert JSON specs to TOML plans:

**Old JSON format (deprecated):**
```json
{
  "project_root": "/path/to/repo",
  "tasks": [
    {"id": "lint-fix", "prompt": "Fix all ruff warnings", "agent": "pi", "touches": ["src/"]},
    {"id": "add-tests", "prompt": "Add unit tests", "agent": "claude", "touches": ["tests/"]}
  ]
}
```

**New TOML plan format:**
```toml
name = "lint-and-test"
description = "Fix lint and add tests"

[[steps]]
name = "lint-fix"
agent = "pi"
prompt = "Fix all ruff warnings"
touches = ["src/"]

[[steps]]
name = "add-tests"
agent = "claude"
prompt = "Add unit tests"
touches = ["tests/"]
```

The plan system provides better dependency management, automatic parallelization, and integration with the kernel's lifecycle management.
