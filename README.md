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

Install from PyPI:

```bash
uv tool install dgov
```

For a local checkout:

```bash
git clone https://github.com/jameshgrn/dgov
cd dgov
uv tool install --from . dgov
dgov agents sync
```

## Quick start

```bash
# 1. Bootstrap your project
cd /path/to/your/repo
# Run inside a git repo. For a new project, initialize git first:
git rev-parse --is-inside-work-tree >/dev/null || git init
dgov init                # Creates .dgov/project.toml, .dgov/governor.md, and .dgov/sops/

# 2. Review bootstrap files and configure the provider placeholders
# .dgov/project.toml: repo toolchain + LLM provider config
# .dgov/governor.md: planning, retry, and done criteria for the governor
# .dgov/sops/*.md: worker execution guidance and review/testing discipline
export PROVIDER_API_KEY=your-key-here

# 3. Create a plan tree
dgov init-plan my-plan      # Scaffolds .dgov/plans/my-plan/_root.toml + tasks/

# 4. Edit .dgov/plans/my-plan/tasks/main.toml, then compile it
dgov compile .dgov/plans/my-plan/

# 5. Run the compiled plan
# If the repo has no commits yet, dgov will create a bootstrap snapshot.
# If the repo has no .sentrux/baseline.json yet, dgov run bootstraps it once.
# Clean complete full-plan runs refresh the accepted baseline after comparison.
dgov run .dgov/plans/my-plan/

# 6. Monitor progress in another terminal
dgov watch
```

For the auto-plan path, replace steps 3–5 with `dgov plan create "<goal>"`. The
planner agent explores the repo and writes a plan tree; add `--run` to compile
and execute it immediately.

## Sentrux Baseline

`dgov` treats `.sentrux/baseline.json` as governor-owned state.

- Create or refresh it explicitly with `dgov sentrux gate-save`
- `dgov run` auto-bootstraps a missing baseline once in a fresh repo or clean worktree
- clean complete full-plan runs refresh accepted sentrux baseline metadata automatically
- worker edits to `.sentrux/baseline.json` and `.sentrux/dgov-baseline.json` are rejected during review
- post-run sentrux degradation marks the run `degraded` and prints a warning

## Knowledge base

This repo includes a source-backed knowledge vault in `docs/knowledge/`.
Articles explain dgov concepts and architecture while citing canonical source
files through frontmatter.

```bash
dgov kb list
dgov kb show sentrux
dgov kb validate
dgov kb graph              # dump article + source graph
dgov kb related <id>       # follow related edges
dgov kb path <from> <to>   # shortest path between articles
dgov kb open <id>          # open an article in Obsidian
```

The KB is explanatory material, not durable memory. Bugs, rules, decisions,
patterns, and debt still belong in `dgov ledger`.

## Agent skills

dgov ships machine-agent skills for ledger, plan, and retired pane guidance.
Install or refresh the local copies with:

```bash
dgov agents sync
```

The canonical source is `agent-guidance/skills/`. Local
`~/.agents/skills/dgov-*` files are derived machine state and should be
refreshed from the command above instead of hand-edited.

## Project configuration

`.dgov/project.toml` carries everything repo-scoped: language, toolchain
commands, LLM providers, tool policy, verification recipes, and coverage knobs.
`dgov init` auto-detects most of it. Task-level `provider = "..."` selects
the endpoint, while `agent = "..."` overrides the model/router name.

### LLM providers

`dgov` talks to OpenAI-compatible HTTP endpoints. Example provider:

```toml
[project]
provider = "llm"

[providers.llm]
default_agent = "provider/model-name"
base_url = "https://provider.example.com/v1"
api_key_env = "PROVIDER_API_KEY"
```

For clients outside `dgov`, use the endpoint shape required by that provider:

```text
base_url = "https://provider.example.com/v1"
model = "provider/model-name"
api_key_env = "PROVIDER_API_KEY"
```

Export the matching env var before `dgov run`.

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
baseline. If a baseline and coverage output are available during settlement,
the gate rejects changed files whose line coverage drops by more than
`coverage_threshold` percentage points.

```toml
coverage_cmd = "uv run pytest --cov=src --cov-report=json:{output} -q"
coverage_threshold = 2.0
```

### Verification recipes

Repeated project-local checks belong in `[verify.<name>]` recipes. Run them
directly with `dgov verify run <name>` or list them with `dgov verify list`.

```toml
[verify.unit]
description = "Run unit tests"
command = "uv run pytest -q -m unit"
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
dgov agents sync           # Install/update shipped dgov agent skills

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
dgov diagnose              # Report matched failure shapes and next actions
dgov ledger add <cat>      # Record bug, rule, pattern, decision, or debt

# Gates
dgov preflight             # Run settlement gates against local changes
dgov verify list           # List project-local verification recipes
dgov verify run <name>     # Run one verification recipe
dgov scope status          # Preview claim/scope settlement status
dgov sentrux check         # Run architectural quality check
dgov sentrux gate-save     # Create or refresh the architectural baseline
dgov sentrux gate          # Compare current state against the baseline
dgov sentrux offenders     # List long/complex function offenders
dgov sentrux status        # Check whether sentrux is available
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
summary = "Add a feature"
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
test_cmd = "uv run pytest -q tests/test_foo.py"
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
| `settlement.py`, `settlement_flow.py` | ruff auto-fix, lint, type-check, tests, sentrux, coverage, integration candidates |
| `semantic_settlement.py` | Deterministic Python semantic checks on integration candidates |
| `tool_policy.py`, `tool_audit.py` | Worker tool allow/deny policy + telemetry audit |
| `policy_drift.py` | Detect drift between canonical policy sources and packaged mirrors |
| `worktree.py` | Git worktree create/merge/remove |
| `plan.py`, `plan_tree.py`, `dag_parser.py` | TOML plan parsing, tree walk, DAG compilation |
| `plan_review.py` | Post-hoc debrief surface for `dgov plan review` |
| `sop_bundler.py` | Load SOPs, pick per unit, prepend to prompts |
| `prompt_builder.py` | Assemble final worker prompts from SOPs + plan context |
| `bootstrap_policy.py`, `bootstrap_policy_data/` | Default SOPs and governor templates for `dgov init` |
| `agent_skills.py`, `agent_skill_data/` | Shipped machine-agent skills for `dgov agents sync` |
| `deploy_log.py` | Append-only JSONL deploy history |
| `archive.py` | Plan archival on success |
| `config.py` | ProjectConfig + `load_project_config()` |
| `persistence/` | SQLite event store, runtime artifact rows, slug history, ledger |
| `cli/` | Click interface |

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q -m unit
uv run pytest -q tests/test_plan.py
```

## License

MIT
