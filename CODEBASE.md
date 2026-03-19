# dgov Codebase Map

## Task routing — start here

| If your task is about... | Start in | Also check | Tests |
|--------------------------|----------|------------|-------|
| Pane create/close/resume | `lifecycle.py` | `done.py`, `gitops.py` | `test_lifecycle.py`, `test_dgov_panes.py` |
| `dgov pane` CLI behavior | `cli/pane.py` | the module it delegates to | `test_cli_pane.py`, `test_dgov_cli.py` |
| Merge/review behavior | `merger.py`, `inspection.py` | `persistence.py` | `test_merger_coverage.py`, `test_merger_conflicts.py`, `test_dgov_merger.py` |
| Retry/escalation/recovery | `recovery.py` | `responder.py`, `monitor.py` | `test_retry.py`, `test_bounded_retry.py`, `test_recovery_dogfood.py` |
| Monitor daemon logic | `monitor.py` | `monitor_hooks.py`, `recovery.py` | `test_monitor.py` |
| Worker completion/done | `cli/worker_cmd.py`, `done.py` | `waiter.py` | `test_done_strategy.py`, `test_dgov_panes.py` |
| Agent routing/selection | `router.py`, `agents.py` | `strategy.py` | `test_router.py`, `test_dgov_agents.py` |
| Prompt templates | `templates.py`, `strategy.py` | `lifecycle.py` | `test_templates.py` |
| Dashboard/terrain TUI | `dashboard.py`, `terrain.py` | `terrain_pane.py` | `test_dashboard.py`, `test_terrain_events.py` |
| DAG/batch/mission | `dag.py`, `batch.py`, `mission.py` | `dag_parser.py`, `dag_graph.py` | `test_dag.py`, `test_batch.py`, `test_mission.py` |
| State DB/events | `persistence.py` | `status.py`, `metrics.py` | `test_dgov_state.py`, `test_persistence_pane.py` |
| Top-level CLI command | matching `cli/*_cmd.py` | `cli/__init__.py` (registration) | `test_cli_admin.py`, `test_dgov_cli.py` |
| Preflight/doctor/tunnel | `preflight.py`, `cli/admin.py` | `agents.py` | `test_dgov_preflight.py`, `test_init_doctor.py` |

## Invariants — do not break these

- You are in a **git worktree**, not the main repo. Do not merge, rebase, or pull.
- `CLAUDE.md` and `AGENTS.md` are **git-excluded** — they exist on disk for you to read but cannot be staged or committed.
- `dgov worker complete` will **auto-commit** any unstaged changes before signaling done.
- Protected files (`CLAUDE.md`, `THEORY.md`, `.napkin.md`) are **restored during merge** — changes to them are discarded.
- Do NOT push to remote. Do NOT run the full test suite.

## Module groups

### Orchestration core
| File | Size | Purpose |
|------|------|---------|
| `lifecycle.py` | L | Pane create, close, cleanup, worktree instructions |
| `persistence.py` | L | SQLite state DB, event journal, dispatch queue |
| `done.py` | M | Done-signal detection, commit checking |
| `gitops.py` | S | Worktree and branch removal helpers |
| `waiter.py` | M | Wait/poll for pane completion |
| `status.py` | M | List panes, prune stale, capture output |

### Merge and review
| File | Size | Purpose |
|------|------|---------|
| `merger.py` | L | Plumbing merge, rebase merge, conflict resolution, post-merge lint+test |
| `inspection.py` | M | Review diffs, compute verdicts, freshness |

### Automation and recovery
| File | Size | Purpose |
|------|------|---------|
| `monitor.py` | L | Background daemon: classify workers, auto-merge, auto-retry, gc |
| `recovery.py` | M | Retry, escalation chain, bounded retry |
| `responder.py` | S | Auto-respond to blocked worker panes |
| `monitor_hooks.py` | S | TOML-based monitor hook configuration |

### Agent integration
| File | Size | Purpose |
|------|------|---------|
| `agents.py` | L | Agent registry, launch command builder |
| `router.py` | S | Logical agent names → physical backends |
| `strategy.py` | S | Slug generation, prompt structuring |
| `templates.py` | S | Prompt template system |
| `openrouter.py` | M | OpenRouter API client with local fallback |

### CLI surface
| File | Size | Purpose |
|------|------|---------|
| `cli/__init__.py` | M | Main CLI group, governor detection, command registration |
| `cli/pane.py` | L | All `dgov pane` subcommands |
| `cli/admin.py` | L | preflight, doctor, status, dashboard, gc, terrain, etc. |
| `cli/worker_cmd.py` | S | `dgov worker complete/fail/checkpoint` |
| `cli/monitor_cmd.py` | S | `dgov monitor` |
| `cli/batch_cmd.py` | S | `dgov batch` |
| `cli/dag_cmd.py` | M | `dgov dag` |
| `cli/*_cmd.py` | S | Other top-level commands (mission, review-fix, etc.) |

### Higher-level workflows
| File | Size | Purpose |
|------|------|---------|
| `mission.py` | M | Declarative create-wait-review-merge lifecycle |
| `batch.py` | M | Batch execution and checkpoint management |
| `dag.py` | L | DAG file parser and execution engine |
| `dag_parser.py` | S | DAG TOML data model |
| `dag_graph.py` | S | DAG graph algorithms |
| `review_fix.py` | M | Dispatch review workers, parse findings, dispatch fixes |
| `experiment.py` | M | Autoresearch-style experiment loops |

### Visualization
| File | Size | Purpose |
|------|------|---------|
| `dashboard.py` | L | Rich TUI for pane management |
| `terrain.py` | L | SPIM erosion terrain model |
| `terrain_pane.py` | S | Standalone terrain pane launcher |

Size key: S = <200 lines, M = 200-500, L = 500+

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
