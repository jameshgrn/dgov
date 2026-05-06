# dgov

Deterministic kernel for multi-agent orchestration via git worktrees.

Docs: https://sandfrom.space/dgov/

## Requirements

- Python 3.12+
- git
- [uv](https://docs.astral.sh/uv/)
- [sentrux](https://github.com/sentrux/sentrux)
- An OpenAI-compatible API endpoint and API key

## Install

From source today:

```bash
git clone https://github.com/jameshgrn/dgov
cd dgov
uv tool install --from . dgov
```

Once published to PyPI:

```bash
uv tool install dgov
```

## Quick start

```bash
# 1. Set your API key
export FIREWORKS_API_KEY=your-key-here

# 2. Bootstrap your project
cd /path/to/your/repo
git init
dgov init                # Creates .dgov/project.toml, .dgov/governor.md, and .dgov/sops/

# 3. Review bootstrap files
# .dgov/project.toml: repo toolchain + LLM endpoint config
# .dgov/governor.md: planning, retry, and done criteria for the governor
# .dgov/sops/*.md: worker execution guidance and review/testing discipline

# 4. Create a plan tree
dgov init-plan my-plan

# 5. Edit .dgov/plans/my-plan/tasks/main.toml, then compile it
dgov compile .dgov/plans/my-plan/

# 6. Run the compiled plan
# If the repo has no commits yet, dgov will create a bootstrap snapshot.
# If the repo has no .sentrux/baseline.json yet, dgov run bootstraps it once,
# then keeps using explicit baseline comparison on subsequent runs.
dgov run .dgov/plans/my-plan/

# 7. Monitor progress in another terminal
dgov watch
```

## Sentrux Baseline

`dgov` treats `.sentrux/baseline.json` as governor-owned state.

- Create or refresh it explicitly with `dgov sentrux gate-save`
- `dgov run` auto-bootstraps a missing baseline once in a fresh repo or clean worktree
- worker tasks must not edit `.sentrux/baseline.json`
- a run fails if the final post-run sentrux comparison reports degradation

## LLM Configuration

`dgov` uses an OpenAI-compatible client. The repo-level endpoint settings live in
`.dgov/project.toml`, and task-level `agent = "..."` values still only override the model/router
name.

Default generated config:

```toml
[project]
default_agent = "accounts/fireworks/routers/kimi-k2p5-turbo"
llm_base_url = "https://api.fireworks.ai/inference/v1"
llm_api_key_env = "FIREWORKS_API_KEY"
```

To use official OpenAI instead:

```toml
[project]
default_agent = "gpt-4.1-mini"
llm_base_url = "https://api.openai.com/v1"
llm_api_key_env = "OPENAI_API_KEY"
```

To use another OpenAI-compatible endpoint:

```toml
[project]
default_agent = "your-model-name"
llm_base_url = "https://your-endpoint.example.com/v1"
llm_api_key_env = "YOUR_PROVIDER_API_KEY"
```

Then export the matching env var before `dgov compile` or `dgov run`.

## SOP Format

`dgov init` scaffolds the policy pack in three layers:
- `.dgov/project.toml`: repo toolchain, runtime, and provider config
- `.dgov/governor.md`: governor planning, retry, and done criteria
- `.dgov/sops/*.md`: worker execution guidance and review/testing discipline

SOP files are standardized:
- required front matter: `name`, `title`, `summary`, `applies_to`, `priority`
- required sections: `When`, `Do`, `Do Not`, `Verify`, `Escalate`

`dgov compile` validates SOP structure and fails closed on malformed files.

## How it works

dgov dispatches tasks to AI coding agents running in isolated git worktrees. Each worker gets its own branch and subprocess. Plans are defined in TOML, compiled to DAGs, and executed through a pure kernel with event-sourced lifecycle state.

State is stored in `.dgov/state.db` (SQLite WAL). The event log is the authority for lifecycle state; runtime artifact rows are best-effort bookkeeping for worktrees, branches, and related execution metadata. Workers are subprocess-isolated via an OpenAI-compatible API client.

## Usage

```bash
dgov                     # Show status
dgov status              # Show status (explicit)
dgov --json status       # Show status as JSON
dgov init                # Bootstrap .dgov/project.toml, .dgov/governor.md, and .dgov/sops/
dgov init-plan <name>    # Initialize a new plan directory
dgov fix <prompt>        # Create and run a single-task fix plan
dgov compile <dir>       # Compile a plan tree to _compiled.toml
dgov run <dir>           # Compile and run a plan directory
dgov run --continue <dir> # Retry failed/abandoned tasks after a transient failure
dgov validate plan.toml  # Validate a plan without running
dgov watch               # Stream events live
dgov plan status <dir>   # Show pending vs deployed units
dgov archive-plan <name> # Manually archive a plan to .dgov/plans/archive/
dgov ledger add <cat>    # Record bug, rule, or debt
dgov clean               # Clean stale worktrees and output directories
dgov prune               # Remove historical runtime artifact rows
dgov sentrux check       # Run architectural quality check
dgov sentrux gate-save   # Create or refresh the explicit baseline
dgov sentrux gate        # Compare current state against that baseline
```

## Plan format

Plans are authored as plan trees under `.dgov/plans/<name>/` and compiled to DAGs:

```toml
# .dgov/plans/example/_root.toml
[plan]
name = "example"
summary = "Add a feature safely"
sections = ["tasks"]

# .dgov/plans/example/tasks/main.toml
[tasks.add-feature]
summary = "Add the feature"
prompt = "Add the feature to src/foo.py"
commit_message = "feat: add feature"
files.edit = ["src/foo.py"]

[tasks.add-tests]
summary = "Write tests"
prompt = "Write tests for the feature"
commit_message = "test: add tests for feature"
files.edit = ["tests/test_foo.py"]
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
| `persistence/` | SQLite event store, runtime artifact rows, and slug history |
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

## License

MIT
