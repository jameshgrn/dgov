# dgov

Deterministic kernel for multi-agent orchestration via git worktrees.

## Architecture (src/dgov/)

| Module | Role |
|--------|------|
| `kernel.py` | Pure function: `(state, event) -> (new_state, actions)`. No I/O. |
| `actions.py` | Frozen dataclass command/event vocabulary |
| `types.py` | `TaskState` enum — single source of truth for state |
| `runner.py` | `EventDagRunner` — async bridge feeding kernel |
| `worker.py` | Standalone OpenAI-client subprocess (Fireworks/Kimi) |
| `workers/headless.py` | Subprocess launcher, JSON event stream |
| `settlement.py` | ruff auto-fix + lint gate + sentrux policy gate |
| `worktree.py` | create/merge/remove git worktrees (FF + cherry-pick) |
| `plan.py` | PlanSpec/PlanUnit, TOML parse, compile to DAG |
| `dag_parser.py` | Pydantic v2 models, TOML → DagDefinition |
| `config.py` | ProjectConfig + load_project_config |
| `persistence/` | SQLite (tasks + events + slug_history), WAL mode |
| `cli/` | Click CLI: `dgov [status \| run \| validate \| init \| watch \| sentrux]` |

## Principles

- **Kernel is pure** — no persistence imports, no I/O
- **Workers are subprocess-isolated** — own worktree, own branch
- **Settlement = commit-or-kill** — ruff + lint + sentrux gates
- **Event-sourced** — all state transitions logged to SQLite

## Dev workflow

```bash
uv pip install -e .          # editable install
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pytest tests/ -q      # full suite (~250 tests, <10s)
```

## Testing

- `tests/test_kernel.py` — kernel state machine
- `tests/test_runner.py` — DAG runner async logic
- `tests/test_plan.py` — TOML plan parsing + compilation
- `tests/test_dag_parser.py` — DAG definition parsing
- `tests/test_settlement.py` — settlement pipeline
- `tests/test_boundaries.py` — import boundary enforcement
- `tests/test_integration.py` — end-to-end with real git repos
- `tests/test_tasks.py` — persistence CRUD
- `tests/test_types.py` — type/enum validation
- `tests/test_cli.py` — CLI commands (status, validate, init, watch)

Markers: `unit`, `integration`. Use `-m unit` for fast feedback.
