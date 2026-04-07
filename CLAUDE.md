# dgov

Deterministic kernel for multi-agent orchestration via git worktrees.

## Standards

Canonical project standards are maintained as Standard Operating Procedures (SOPs)
in `.dgov/sops/`. These are dynamically prepended to worker prompts at compile time:

- `architecture.md` — Pure kernel, event-sourcing, isolation
- `python-style.md` — Toolchain (`uv`, `ruff`, `ty`), zero-warnings
- `testing.md` — Execution (`pytest -q`), methodology (behavior over implementation)
- `git-commits.md` — Feature branches, atomic commits, imperative mood
- `error-handling.md` — Fail-fast, no silent failures, replace don't deprecate

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

## Testing Map

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
