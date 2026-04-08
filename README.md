# dgov

Deterministic kernel for multi-agent orchestration via git worktrees.

## Install

```bash
uv pip install -e .
```

Requires Python 3.12+, git.

For development (includes ruff, pytest, mypy):

```bash
uv pip install -e . --group dev
```

## How it works

dgov dispatches tasks to AI coding agents running in isolated git worktrees. Each worker gets its own branch and subprocess. Plans are defined in TOML, compiled to DAGs, and executed through a pure kernel with event-sourced state.

State is stored in `.dgov/state.db` (SQLite WAL). Workers are subprocess-isolated via OpenAI-compatible APIs (Fireworks, Kimi).

## Usage

```bash
dgov                     # Show status
dgov status              # Show status (explicit)
dgov run plan.toml       # Execute a plan
dgov validate plan.toml  # Validate a plan without running
dgov watch               # Stream events
dgov sentrux check       # Run architectural quality check
dgov sentrux gate-save   # Save quality baseline
dgov sentrux gate        # Compare against baseline
dgov plan status <dir>   # Show pending vs deployed units
```

## Plan format

Plans are TOML files that compile to DAGs:

```toml
[plan]
name = "example"

[tasks.add-feature]
prompt = "Add the feature to src/foo.py"
agent = "kimi-k2.5"
files = ["src/foo.py"]

[tasks.add-tests]
prompt = "Write tests for the feature"
agent = "kimi-k2.5"
files = ["tests/test_foo.py"]
depends_on = ["add-feature"]
```

## Architecture

| Module | Role |
|--------|------|
| `kernel.py` | Pure `(state, event) → (new_state, actions)` — no I/O |
| `runner.py` | Async DAG executor feeding the kernel |
| `worker.py` | Standalone OpenAI-client subprocess |
| `settlement.py` | ruff auto-fix + lint gate + sentrux policy gate |
| `worktree.py` | Git worktree create/merge/remove |
| `plan.py` | TOML plan parsing, DAG compilation |
| `persistence/` | SQLite event store (panes + events + slug history) |

## Development

```bash
uv pip install -e . --group dev
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pytest tests/ -q
```

## License

MIT
