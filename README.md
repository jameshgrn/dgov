# dgov

Deterministic kernel for multi-agent orchestration via git worktrees.

## Requirements

- Python 3.12+
- git
- A [Fireworks AI](https://fireworks.ai) API key (set `FIREWORKS_API_KEY`)

## Install

```bash
# From PyPI
pip install dgov

# From source
git clone https://github.com/jameshgrn/dgov
cd dgov
uv sync
```

For development (includes ruff, pytest, ty):

```bash
uv sync --group dev
```

## Quick start

```bash
# 1. Set your API key
export FIREWORKS_API_KEY=your-key-here

# 2. Bootstrap your project
cd /path/to/your/repo
dgov init

# 3. Author a plan and compile it
dgov compile .dgov/plans/my-plan/

# 4. Run the plan
dgov run .dgov/plans/my-plan/

# 5. Monitor progress in another terminal
dgov watch
```

## How it works

dgov dispatches tasks to AI coding agents running in isolated git worktrees. Each worker gets its own branch and subprocess. Plans are defined in TOML, compiled to DAGs, and executed through a pure kernel with event-sourced state.

State is stored in `.dgov/state.db` (SQLite WAL). Workers are subprocess-isolated via OpenAI-compatible APIs (Fireworks, Kimi).

## Usage

```bash
dgov                     # Show status
dgov status              # Show status (explicit)
dgov init                # Bootstrap .dgov/project.toml
dgov init-plan <name>    # Initialize a new plan directory
dgov fix <prompt>        # Create and run a single-task fix plan
dgov compile <dir>       # Compile a plan tree to _compiled.toml
dgov run plan.toml       # Execute a compiled plan
dgov validate plan.toml  # Validate a plan without running
dgov watch               # Stream events live
dgov plan status <dir>   # Show pending vs deployed units
dgov archive-plan <name> # Manually archive a plan to .dgov/plans/archive/
dgov ledger add <cat>    # Record bug, rule, or debt
dgov clean               # Clean stale worktrees and output directories
dgov recover             # Recover from a crashed run (mark orphaned tasks abandoned)
dgov prune               # Remove historical task records
dgov sentrux check       # Run architectural quality check
dgov sentrux gate-save   # Save quality baseline
dgov sentrux gate        # Compare against baseline
```

## Plan format

Plans are TOML files that compile to DAGs:

```toml
[plan]
name = "example"

[tasks.add-feature]
summary = "Add the feature"
prompt = "Add the feature to src/foo.py"
commit_message = "feat: add feature"
files = ["src/foo.py"]

[tasks.add-tests]
summary = "Write tests"
prompt = "Write tests for the feature"
commit_message = "test: add tests for feature"
files = ["tests/test_foo.py"]
depends_on = ["add-feature"]
```

## Architecture

| Module | Role |
|--------|------|
| `kernel.py` | Pure `(state, event) → (new_state, actions)` — no I/O |
| `runner.py` | Async DAG executor feeding the kernel |
| `worker.py` | Standalone OpenAI-client subprocess |
| `workers/` | Worker tools (read, write, edit, run_bash, grep, etc.) |
| `settlement.py` | ruff auto-fix + lint gate + sentrux policy gate |
| `worktree.py` | Git worktree create/merge/remove |
| `plan.py` | TOML plan parsing, DAG compilation |
| `plan_tree.py` | Walker + merger + resolver + validator |
| `dag_parser.py` | Pydantic v2 models, TOML → DagDefinition |
| `sop_bundler.py` | Load SOPs, pick per unit, prepend to prompts |
| `deploy_log.py` | Append-only JSONL deploy history |
| `config.py` | ProjectConfig + `load_project_config()` |
| `persistence/` | SQLite event store (tasks + events + slug history) |
| `cli/` | Click interface |

## Development

```bash
uv sync --group dev
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run ty check
uv run pytest -q -m unit
uv run pytest -q -m integration
```

## Known Limitations

- **No resume/checkpoint**: if a run crashes at task N, all N tasks restart. Use `dgov run --continue` to skip already-merged tasks.
- **No cost tracking**: token usage and API cost are not recorded.
- **Parallel contention**: running 6+ tasks in parallel may cause contention in the executor. Keep parallelism ≤5 for now.
- **Worker iteration cap**: workers are capped at 30 tool-call iterations. Tasks requiring many read/fix/test cycles may time out.
- **Fireworks AI / OpenAI-compat only**: the worker requires an OpenAI-compatible API. Native Anthropic and other providers are not supported.

## License

MIT
