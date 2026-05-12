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

`dgov` is not published on PyPI yet. Install from source:

```bash
git clone https://github.com/jameshgrn/dgov
cd dgov
uv tool install --from . dgov
```

## Quick start

```bash
# 1. Set your API key
export FIREWORKS_API_KEY=your-key-here

# 2. Bootstrap your project
cd /path/to/your/repo
# Run inside a git repo. For a new project, initialize git first:
git rev-parse --is-inside-work-tree >/dev/null || git init
dgov init                # Creates .dgov/project.toml, .dgov/governor.md, and .dgov/sops/

# 3. Review bootstrap files
# .dgov/project.toml: repo toolchain + LLM endpoint config
# .dgov/governor.md: planning, retry, and done criteria for the governor
# .dgov/sops/*.md: worker execution guidance and review/testing discipline

# 4. Create a plan tree
dgov init-plan my-plan      # Scaffolds .dgov/plans/my-plan/_root.toml + tasks/

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

For the auto-plan path, replace steps 4–6 with `dgov plan create "<goal>"`. The
planner agent explores the repo and writes a plan tree; add `--run` to compile
and execute it immediately.

## Sentrux Baseline

`dgov` treats `.sentrux/baseline.json` as governor-owned state.

- Create or refresh it explicitly with `dgov sentrux gate-save`
- `dgov run` auto-bootstraps a missing baseline once in a fresh repo or clean worktree
- worker tasks must not edit `.sentrux/baseline.json`
- a run fails if the final post-run sentrux comparison reports degradation

## Project configuration

`.dgov/project.toml` carries everything repo-scoped: language, toolchain
commands, LLM endpoint, tool policy, and coverage knobs. `dgov init`
auto-detects most of it. Task-level `agent = "..."` overrides the model/router
name only.

### LLM endpoint

`dgov` talks to an OpenAI-compatible HTTP endpoint. Default:

```toml
[project]
default_agent = "accounts/fireworks/routers/kimi-k2p6-turbo"
llm_base_url = "https://api.fireworks.ai/inference/v1"
llm_api_key_env = "FIREWORKS_API_KEY"
```

For Anthropic-compatible clients outside `dgov`, use the same Fireworks router
with the Anthropic-compatible endpoint:

```text
base_url = "https://api.fireworks.ai/inference"
model = "accounts/fireworks/routers/kimi-k2p6-turbo"
api_key_env = "FIREWORKS_API_KEY"
```

Export the matching env var before `dgov compile` or `dgov run`.

### Tool policy

`[tool_policy]` constrains what worker subprocesses may shell out to:

```toml
[tool_policy]
restrict_run_bash = true
deny_shell_commands = ["pip", "python -m pip", "pip3", "python -m venv", "uv venv"]
deny_shell_file_mutations = true
require_wrapped_verify_tools = true
require_uv_run = true
```

### Coverage

Optional. When `coverage_cmd` is set, `dgov coverage-baseline` records a
baseline and the settlement gate fails a run if line coverage drops by more
than `coverage_threshold` percentage points.

```toml
coverage_cmd = "uv run pytest --cov=src --cov-report=json:{output} -q"
coverage_threshold = 2.0
```

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
# Status and bootstrap
dgov                       # Show status
dgov status                # Show status (explicit)
dgov --json status         # Show status as JSON
dgov init                  # Bootstrap .dgov/project.toml, governor.md, sops/

# Plans
dgov init-plan <name>      # Scaffold an empty plan tree
dgov plan create "<goal>"  # Auto-generate a plan tree via the planner agent
dgov compile <dir>         # Compile a plan tree to _compiled.toml
dgov validate <plan>       # Parse a plan without running
dgov run <dir>             # Compile and run a plan directory
dgov run --continue <dir>  # Retry failed/abandoned tasks from the prior run
dgov run --only <task>     # Run a single task and its deps
dgov fix <prompt>          # Create and run a one-off single-task fix plan
dgov plan status <dir>     # Show pending vs deployed units
dgov plan review <dir>     # Post-hoc debrief of the last run
dgov plan remediate <dir>  # Scaffold a follow-up plan for a degraded deploy
dgov archive-plan <name>   # Move a plan to .dgov/plans/archive/

# Observability
dgov watch                 # Stream events live
dgov tools audit           # Summarize worker tool-call telemetry
dgov ledger add <cat>      # Record bug, rule, pattern, decision, or debt

# Gates
dgov preflight             # Run settlement gates against local changes
dgov sentrux check         # Run architectural quality check
dgov sentrux gate-save     # Create or refresh the architectural baseline
dgov sentrux gate          # Compare current state against the baseline
dgov sentrux offenders     # List long/complex function offenders
dgov coverage-baseline     # Record or refresh the coverage baseline

# Maintenance
dgov clean                 # Clean stale worktrees and output dirs
dgov prune                 # Remove historical runtime artifact rows
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
prompt = '''
Orient:
Read src/foo.py before editing. Keep the public API unchanged.

Edit:
1. Add the feature behavior in src/foo.py.
2. Keep the change scoped to the existing module.

Verify:
uv run ruff check src/foo.py
uv run ty check
'''
commit_message = "feat: add feature"
files.edit = ["src/foo.py"]

[tasks.add-tests]
summary = "Write tests"
prompt = '''
Orient:
Read src/foo.py and the existing test style before editing. Test behavior, not implementation.

Edit:
1. Add coverage for the new feature in tests/test_foo.py.
2. Include the main success path and one error or edge case.

Verify:
uv run ruff check tests/test_foo.py
uv run pytest -q tests/test_foo.py
'''
commit_message = "test: add tests for feature"
files.create = ["tests/test_foo.py"]
files.read = ["src/foo.py"]
depends_on = ["add-feature"]
```

## Architecture

| Module | Role |
|--------|------|
| `kernel.py` | Pure `(state, event) → (new_state, actions)` — no I/O |
| `runner.py` | Async DAG executor feeding the kernel |
| `worker.py` | Standalone OpenAI-client subprocess |
| `workers/` | Worker tools (read, write, edit, run_bash, grep, etc.) |
| `planner.py` | Auto-plan generator agent (powers `dgov plan create`) |
| `researcher.py` | Read-only research role driver |
| `settlement.py`, `settlement_flow.py` | ruff auto-fix + lint gate + sentrux + coverage gate |
| `semantic_settlement.py` | LLM-driven semantic verdict on worker output |
| `tool_policy.py`, `tool_audit.py` | Worker tool allow/deny policy + telemetry audit |
| `policy_drift.py` | Detect drift between policy and observed worker behavior |
| `worktree.py` | Git worktree create/merge/remove |
| `plan.py`, `plan_tree.py`, `dag_parser.py` | TOML plan parsing, tree walk, DAG compilation |
| `plan_review.py` | Post-hoc debrief surface for `dgov plan review` |
| `sop_bundler.py` | Load SOPs, pick per unit, prepend to prompts |
| `prompt_builder.py` | Assemble final worker prompts from SOPs + plan context |
| `bootstrap_policy.py`, `bootstrap_policy_data/` | Default SOPs and governor templates for `dgov init` |
| `deploy_log.py` | Append-only JSONL deploy history |
| `archive.py` | Plan archival on success |
| `config.py` | ProjectConfig + `load_project_config()` |
| `persistence/` | SQLite event store, runtime artifact rows, slug history, ledger |
| `cli/` | Click interface |

## Development

```bash
uv sync --group dev
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run ty check
uv run pytest -q -m unit
uv run pytest -q tests/test_plan.py
```

## License

MIT
