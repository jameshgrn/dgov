# dgov — Development Guide

## Toolchain
- **Environment**: `uv` (use `uv run` prefix)
- **Linting**: `ruff check .`
- **Formatting**: `ruff format .`
- **Type Checking**: `ty check` (via `uv run ty check`)
- **Testing**: `pytest -q` (use `-m unit` or `-m integration`)

## Architecture (src/dgov/)

| Module | Role |
|--------|------|
| `kernel.py` | Pure state machine: `(state, event) -> (new_state, actions)` |
| `actions.py` | Command/Event vocabulary (frozen dataclasses) |
| `types.py` | `TaskState` enum and shared types |
| `runner.py` | Async bridge feeding the kernel; manages subprocesses and worktrees |
| `worker.py` | Compute engine (OpenAI client) running in subprocess |
| `settlement.py` | The "Auditor": ruff auto-fix + lint gate + sentrux policy gate |
| `persistence/` | SQLite event-store and task tracking (WAL mode) |
| `cli/` | Click interface (`status`, `run`, `compile`, `init`, `watch`) |

## Testing Map
- `tests/test_kernel.py`: State machine transitions
- `tests/test_runner.py`: Async orchestration logic
- `tests/test_integration.py`: End-to-end lifecycle with real git repos
- `tests/test_settlement.py`: Validation gate pipeline
- `tests/test_tasks.py`: Persistence CRUD
