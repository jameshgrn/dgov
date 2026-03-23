# dgov Codebase Map

## Task routing — start here

| If your task is about... | Start in | Also check | Tests |
|--------------------------|----------|------------|-------|
| Pane create/close/resume | `lifecycle.py` | persistence.py, done.py, gitops.py | test_lifecycle.py |
| Merge/review behavior | `merger.py` | inspection.py, persistence.py | test_merger*.py |
| Review diffs, verdicts, freshness | `inspection.py` | merger.py | test_inspection*.py |
| Retry/escalation/recovery | `recovery.py` | responder.py, monitor.py | test_retry*.py |
| Monitor daemon logic | `monitor.py` | monitor_hooks.py, recovery.py | test_monitor.py |
| Worker completion/done | `done.py, waiter.py` | lifecycle.py | test_done_strategy.py |
| Agent routing/selection | `router.py, agents.py` | strategy.py | test_router.py |
| Decision providers | `decision.py, decision_providers.py` | provider_registry.py | test_decision.py |
| Prompt templates | `templates.py, strategy.py` | lifecycle.py | test_templates.py |
| Dashboard/terrain TUI | `dashboard.py, terrain.py` | terrain_pane.py | test_dashboard.py |
| DAG/batch/mission | `dag.py, batch.py, mission.py` | dag_parser.py, dag_graph.py | test_dag.py, test_batch.py, test_mission.py |
| State DB/events | `persistence.py` | status.py, metrics.py | test_persistence*.py |
| Top-level CLI command | `cli/admin.py, cli/pane.py` | cli/__init__.py | test_cli_admin.py, test_dgov_cli.py |

## Invariants — do not break these

- You are in a **git worktree**, not the main repo. Do not merge, rebase, or pull.
- `CLAUDE.md` and `AGENTS.md` are **git-excluded** — exist on disk for read, cannot commit.
- `dgov worker complete` will **auto-commit** any unstaged changes before signaling done.
- Protected files (CLAUDE.md, THEORY.md) **restored during merge** — changes discarded.
- Do NOT push to remote. Do NOT run the full test suite.

## Module groups

### Orchestration core
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/done.py` | L | Done-signal and done-detection helpers. |
| `src/dgov/executor.py` | L | Shared executor policy for dispatch preflight and merge review gates. |
| `src/dgov/gitops.py` | S | Low-level git plumbing helpers for worktree and branch management. |
| `src/dgov/kernel.py` | L | Deterministic kernel primitives for pane and DAG lifecycle. |
| `src/dgov/lifecycle.py` | L | Pane lifecycle: create, close, resume, and cleanup. |
| `src/dgov/persistence.py` | L | State file management and event journal. |
| `src/dgov/status.py` | L | Pane status: list, freshness, output capture, pruning. |
| `src/dgov/waiter.py` | L | Wait/poll logic for worker panes. |

### Merge and review
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/inspection.py` | L | Pane inspection: review, diff, rebase. |
| `src/dgov/merger.py` | L | Git merge, conflict resolution, and post-merge operations. |

### Automation and recovery
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/monitor.py` | L | Lightweight polling daemon for worker state classification and auto-remediation. |
| `src/dgov/monitor_hooks.py` | S | Configurable monitor hooks via TOML configuration files. |
| `src/dgov/recovery.py` | L | Pane recovery: retry policy, escalation, and bounded retry with auto-escalation. |
| `src/dgov/responder.py` | S | Auto-respond to blocked worker panes. |

### Agent integration
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/agents.py` | L | Agent registry and launch command builder. |
| `src/dgov/cli/templates.py` | S | Prompt template commands. |
| `src/dgov/openrouter.py` | M | OpenRouter API client with local Qwen 4B fallback. |
| `src/dgov/router.py` | S | Agent router: resolve logical model names to available physical backends. |
| `src/dgov/strategy.py` | M | Task routing, slug generation, and prompt structuring. |
| `src/dgov/templates.py` | S | Prompt template system for worker panes. |

### Decision system
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/context_packet.py` | S | Compiled task context shared across preflight, prompts, and instructions. |
| `src/dgov/decision.py` | L | Typed decision requests, records, and provider wrappers. |
| `src/dgov/decision_providers.py` | M | Concrete decision providers built on existing dgov transports. |
| `src/dgov/provider_registry.py` | S | Central provider selection and optional decision journaling. |

### Higher-level workflows
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/batch.py` | M | Batch execution and checkpoint management. |
| `src/dgov/dag.py` | M | DAG file parser and execution engine for dgov. |
| `src/dgov/dag_graph.py` | S | DAG graph algorithms: validation, topological sort, tier computation. |
| `src/dgov/dag_parser.py` | S | DAG file dataclasses and TOML parser. |
| `src/dgov/review_fix.py` | M | Review-then-fix pipeline: dispatch review workers, parse findings, dispatch fix workers. |

### Other
| File | Size | Purpose |
|------|------|---------|
| `src/dgov/api.py` | M | Public Python API for dgov. |
| `src/dgov/backend.py` | M | Abstract worker backend interface and tmux implementation. |
| `src/dgov/blame.py` | M | Blame: query event journal + git history to attribute file changes to agents. |
| `src/dgov/cli/admin.py` | L | Administrative and diagnostic commands. |
| `src/dgov/cli/batch_cmd.py` | S | Checkpoint and batch commands. |
| `src/dgov/cli/briefing_cmd.py` | S | CLI command: dgov briefing — on-demand document viewer via glow. |
| `src/dgov/cli/dag_cmd.py` | M | CLI commands for DAG execution. |
| `src/dgov/cli/journal_cmd.py` | S | Decision journal query CLI. |
| `src/dgov/cli/ledger_cmd.py` | S | Operational ledger CLI — formalized napkin. |
| `src/dgov/cli/merge_queue_cmd.py` | S | Merge queue commands for governor-side queue processing. |
| `src/dgov/cli/monitor_cmd.py` | S | Monitor daemon CLI command. |
| `src/dgov/cli/openrouter_cmd.py` | S | OpenRouter integration commands. |
| `src/dgov/cli/pane.py` | L | Pane management commands. |
| `src/dgov/cli/plan_cmd.py` | S | CLI commands for dgov plan execution. |
| `src/dgov/cli/review_fix_cmd.py` | S | Review-fix pipeline command. |
| `src/dgov/cli/trace_cmd.py` | M | Span and tool-trace CLI commands. |
| `src/dgov/cli/wait_cmd.py` | S | CLI command for event-driven waiting and governor interrupts. |
| `src/dgov/cli/worker_cmd.py` | S | Worker status reporting commands. |
| `src/dgov/dashboard.py` | L | Rich-based live dashboard for dgov pane management. |
| `src/dgov/plan.py` | L | Plan schema, validator, and compiler for dgov. |
| `src/dgov/preflight.py` | L | Pre-flight validation for dgov dispatch. |
| `src/dgov/spans.py` | L | Structured span and tool-trace observability for dgov. |
| `src/dgov/terrain.py` | L | SPIM erosion terrain model for dgov dashboard. |
| `src/dgov/terrain_pane.py` | S | Standalone terrain simulation pane for dgov governor workspace. |
| `src/dgov/tmux.py` | L | Thin wrappers around tmux commands. |

## CLI command registration

**Pane subcommands** (no registration needed):
1. Add `@pane.command("name")` function to `cli/pane.py`

**Top-level commands**:
1. Add function to appropriate `cli/*.py` file (or create a new `cli/foo_cmd.py`)
2. Import in `cli/__init__.py` (alphabetical)
3. Add `cli.add_command(your_cmd)` after the import block

## Data flow

```
create_worker_pane()
  → load_registry() + resolve_agent()   # find and route agent
  → get_backend().create_worker_pane()   # tmux split-pane
  → add_pane()                           # write to state.db
  → _write_worktree_instructions()       # inject worker context
  → _wrap_done_signal()                  # setup done detection

merge_worker_pane()
  → get_pane()                           # read pane record
  → _restore_protected_files()           # fix CLAUDE.md on branch
  → _commit_worktree()                   # auto-commit uncommitted
  → _rebase_onto_head()                  # rebase branch
  → _plumbing_merge()                    # in-memory git merge
  → _full_cleanup()                      # kill pane + remove worktree
  → _lint_fix_merged_files()             # ruff check + format
  → _run_related_tests()                 # pytest on changed files
```

## State machine

```
active → done → merged → (removed)
active → timed_out → (retry) → active
active → failed → (retry) → active
active → abandoned → closed
any terminal state → closed
```


## Test Mapping

Source file → test files (from .test-manifest.json):

| Source | Tests |
|--------|-------|
| `src/dgov/agents.py` | `test_dgov_agents.py`, `test_dgov_preflight.py`, `test_circuit_breaker.py` +2 more |
| `src/dgov/backend.py` | `test_backend.py`, `test_cascade_close.py`, `test_circuit_breaker.py` +4 more |
| `src/dgov/batch.py` | `test_batch.py`, `test_batch_dag.py` |
| `src/dgov/blame.py` | `test_blame.py`, `test_dgov_blame.py` |
| `src/dgov/cli/__init__.py` | `test_dgov_cli.py`, `test_cli_admin.py`, `test_dgov_state.py` +1 more |
| `src/dgov/cli/admin.py` | `test_cli_admin.py`, `test_dgov_cli.py`, `test_init_doctor.py` |
| `src/dgov/cli/pane.py` | `test_dgov_cli.py`, `test_cli_pane.py`, `test_comms.py` +1 more |
| `src/dgov/cli/templates.py` | `test_templates.py` |
| `src/dgov/context_packet.py` | `test_context_packet.py` |
| `src/dgov/dag.py` | `test_dag.py` |
| `src/dgov/dag_graph.py` | `test_dag_graph.py`, `test_dag_internals.py`, `test_dag.py` |
| `src/dgov/dag_parser.py` | `test_dag_parser.py`, `test_dag_graph.py`, `test_dag_internals.py` +1 more |
| `src/dgov/dashboard.py` | `test_dashboard.py` |
| `src/dgov/decision.py` | `test_decision.py`, `test_decision_providers.py`, `test_monitor.py` +1 more |
| `src/dgov/decision_providers.py` | `test_decision_providers.py`, `test_decision.py`, `test_provider_registry.py` |
| `src/dgov/done.py` | `test_done_strategy.py`, `test_circuit_breaker.py`, `test_comms.py` +1 more |
| `src/dgov/executor.py` | `test_executor.py`, `test_kernel.py`, `test_cli_pane.py` +1 more |
| `src/dgov/gitops.py` | `test_gitops.py` |
| `src/dgov/inspection.py` | `test_inspection.py`, `test_dgov_helpers.py`, `test_integration.py` +1 more |
| `src/dgov/kernel.py` | `test_kernel.py` |
| `src/dgov/lifecycle.py` | `test_lifecycle.py`, `test_executor.py`, `test_dgov_helpers.py` +2 more |
| `src/dgov/merger.py` | `test_dgov_merger.py`, `test_merger_conflicts.py`, `test_merger_coverage.py` +2 more |
| `src/dgov/monitor.py` | `test_monitor.py` |
| `src/dgov/monitor_hooks.py` | `test_monitor_hooks.py` |
| `src/dgov/openrouter.py` | `test_openrouter.py` |
| `src/dgov/persistence.py` | `test_persistence_pane.py`, `test_dgov_state.py`, `test_transitions.py` +15 more |
| `src/dgov/preflight.py` | `test_dgov_preflight.py` |
| `src/dgov/provider_registry.py` | `test_provider_registry.py` |
| `src/dgov/recovery.py` | `test_bounded_retry.py`, `test_recovery_dogfood.py`, `test_retry.py` |
| `src/dgov/responder.py` | `test_responder.py` |
| `src/dgov/review_fix.py` | `test_review_fix.py` |
| `src/dgov/router.py` | `test_router.py` |
| `src/dgov/spans.py` | `test_spans.py` |
| `src/dgov/status.py` | `test_status.py`, `test_preview.py`, `test_dgov_panes.py` |
| `src/dgov/strategy.py` | `test_strategy.py`, `test_dgov_panes.py` |
| `src/dgov/templates.py` | `test_templates.py` |
| `src/dgov/terrain.py` | `test_terrain_events.py` |
| `src/dgov/terrain_pane.py` | `test_terrain_pane.py` |
| `src/dgov/tmux.py` | `test_dgov_tmux.py` |
| `src/dgov/waiter.py` | `test_waiter.py`, `test_comms.py` |