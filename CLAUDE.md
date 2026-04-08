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

---

## Governor Workflow

### Session Loop

1. **START**: Read `HANDOVER.md` + `.napkin.md`. Run `dgov status` to see live task state.
2. **PLAN**: `dgov init-plan <name> --sections <s1,s2,..>`, author TOML units, then `dgov compile .dgov/plans/<name>/` + `dgov validate .dgov/plans/<name>/_compiled.toml`.
3. **RUN**: `dgov run .dgov/plans/<name>/_compiled.toml` -- open a second terminal with `dgov watch` to monitor events live.
4. **RECOVER**: See Recovery Procedures below.
5. **POST-RUN**: `dgov plan status .dgov/plans/<name>/` to track deployment. Run relevant tests. Check `dgov status` for final state.
6. **END**: Run `/handover` to generate `HANDOVER.md`.

### Plan Authoring Guide

- Every task MUST declare file claims. Prefer flat format: `files = ["src/foo.py", "tests/test_foo.py"]`. Use structured format (`files.edit`, `files.create`, `files.delete`) only when explicit deletes are needed. Workers are sandboxed to claimed files -- touching unclaimed files = immediate rejection.
- Keep tasks atomic: one logical change per task.
- `prompt` structure: (1) orient -- what to read first, (2) edit -- exact change and location, (3) verify -- test command to run.
- `depends_on` uses full unit IDs: `<section>/<file-stem>.<task-key>`.
- `commit_message` imperative mood, <=72 chars.
- Compile and validate before running: `dgov compile <dir>` then `dgov validate _compiled.toml`.

### Recovery Procedures

| Situation | Command |
|-----------|---------|
| Tasks failed, rest succeeded | `dgov run <plan> --continue` |
| Crash / orphaned actives | `dgov run <plan>` (orphan cleanup is automatic) |
| Want clean slate | `dgov run <plan> --restart` |
| History noise in status | `dgov prune` |
| Worktree debris | `dgov cleanup` |

### Failure Diagnosis

When a run exits with failed tasks, the output includes `failed_tasks` and `task_errors`.
Cross-check with `dgov watch` log or `.dgov/runs.log` for per-task last_error detail.
