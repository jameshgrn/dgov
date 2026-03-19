# dgov Codebase Map

## Architecture

```
CLI (cli/)  →  Lifecycle  →  Backend (tmux)
                  ↓              ↓
             Persistence    Worker Pane
                  ↓
         Monitor / Recovery
```

## Core modules

| File | Purpose | Key functions |
|------|---------|---------------|
| `lifecycle.py` | Create, close, cleanup worker panes | `create_worker_pane()`, `close_worker_pane()`, `_full_cleanup()` |
| `persistence.py` | SQLite state DB, event journal | `get_pane()`, `update_pane_state()`, `emit_event()`, `all_panes()` |
| `merger.py` | Git merge strategies, conflict resolution | `merge_worker_pane()`, `_plumbing_merge()`, `_rebase_merge()` |
| `inspection.py` | Review diffs, compute verdicts | `review_worker_pane()` |
| `recovery.py` | Retry, escalation chain | `retry_worker_pane()`, `escalate_worker_pane()`, `maybe_auto_retry()` |
| `monitor.py` | Background daemon: classify, auto-merge, auto-retry | `run_monitor()` |
| `router.py` | Logical agent names → physical backends | `resolve_agent()`, `is_routable()` |
| `agents.py` | Agent registry, launch command builder | `load_registry()`, `build_launch_command()` |
| `backend.py` | Abstract tmux interface | `TmuxBackend`, `get_backend()` |
| `tmux.py` | Raw tmux command wrappers | `split_pane()`, `send_command()`, `wait_for_shell_ready()` |
| `status.py` | List panes, prune stale, capture output | `list_worker_panes()`, `prune_stale_panes()` |
| `done.py` | Done-signal detection | `_is_done()`, `_has_new_commits()` |
| `waiter.py` | Wait/poll for pane completion | `wait_for_slugs()` |
| `strategy.py` | Slug generation, prompt structuring | `_generate_slug()`, `_structure_pi_prompt()` |
| `dashboard.py` | Rich TUI for pane management | `_build_worker_table()`, `run_dashboard()` |
| `terrain.py` | SPIM erosion terrain model | `TerrainModel` |

## CLI structure

```
cli/__init__.py          — main CLI group, governor detection, session setup
cli/pane.py              — dgov pane {create,list,wait,review,merge,close,land,...}
cli/admin.py             — dgov {preflight,doctor,status,dashboard,gc,...}
cli/worker_cmd.py        — dgov worker {complete,fail,progress}
cli/monitor_cmd.py       — dgov monitor
cli/batch_cmd.py         — dgov batch
cli/dag_cmd.py           — dgov dag
```

### Adding a CLI command

1. Add the function to the appropriate `cli/*.py` file
2. Import it in `cli/__init__.py` (alphabetical in the import block)
3. Register with `cli.add_command(your_cmd)` (after the import block)

## Data flow

```
create_worker_pane()
  → agents.load_registry()           # find agent config
  → router.resolve_agent()           # logical → physical name
  → backend.create_pane()            # tmux split-pane
  → persistence.insert_pane()        # write to state.db
  → _write_worktree_instructions()   # write worker CLAUDE.md
  → done._wrap_done_signal()         # setup done detection

merge_worker_pane()
  → persistence.get_pane()           # read pane record
  → _restore_protected_files()       # fix CLAUDE.md on branch
  → _commit_worktree()               # auto-commit uncommitted
  → _rebase_onto_head()              # rebase branch
  → _plumbing_merge()                # in-memory git merge
  → _full_cleanup()                  # kill pane + remove worktree
  → _lint_fix_merged_files()         # ruff check + format
  → _run_related_tests()             # pytest on changed files
```

## State machine

```
active → done → merged
active → timed_out → (retry) → active
active → failed → (retry) → active
active → abandoned → closed
done → merged → (removed from DB)
```

## Key constants

- `PROTECTED_FILES`: `{"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md", ".napkin.md"}`
- `STATE_DIR`: `.dgov`
- Worktrees live in: `.dgov/worktrees/<slug>/`
- State DB: `.dgov/state.db`
- Event journal: `.dgov/events.jsonl`

## Test structure

- `tests/test_lifecycle.py` — pane create/close
- `tests/test_merger_coverage.py` — merge strategies
- `tests/test_merger_conflicts.py` — conflict handling
- `tests/test_dgov_panes.py` — comprehensive pane operations
- `tests/test_dashboard.py` — dashboard rendering
- `tests/test_integration.py` — full lifecycle (marked `integration`)
- `tests/test_monitor.py` — monitor daemon
- `tests/test_recovery.py` — retry/escalation

## Common edit patterns

**Add a new pane subcommand:**
1. Edit `cli/pane.py` — add `@pane.command("name")` function
2. No registration needed (it's a subcommand of the `pane` group)

**Add a new top-level command:**
1. Edit `cli/admin.py` — add `@click.command("name")` function
2. Edit `cli/__init__.py` — add to import block + `cli.add_command()`

**Add a new event type:**
1. Edit `persistence.py` — add to `EventType` if enum exists, or just emit it
2. Update tests that assert on event type lists

**Modify merge behavior:**
1. Edit `merger.py` — the merge strategies are `_plumbing_merge`, `_rebase_merge`, `_no_squash_merge`
2. Post-merge hooks are inline at the end of `merge_worker_pane()`
