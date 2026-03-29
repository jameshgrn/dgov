# Handover: dgov routing + executor bugs fixed

## Session context
- Date: 2026-03-29
- Branch: main
- Last commit: f8fa722 - fix(routing): correct role hierarchy and precedence

## Open work
- **None** - all panes completed and merged.

## Open bugs/issues (from ledger)
- **None** - all bugs resolved and closed:
  - #184: Cancel retry descendants on DAG cancel (FIXED)
  - #185: Emit pane_timed_out event on timeout (FIXED)
  - #199: Project-local agents.toml routing errors (FIXED)

## Completed work

### Bug fixes
1. **src/dgov/executor.py**: Two bug fixes
   - `run_cancel_dag()`: Now recursively closes retry descendant panes via `get_child_panes()`
   - `_dag_wait_any()`: Now emits `pane_timed_out` events before returning on timeout
   - Regression tests: `tests/test_executor_bugs_184_185.py`

2. **src/dgov/agents.py**: Fixed routing table precedence
   - `load_routing_tables()` now correctly loads user-global first, then project-local overrides
   - Previously was inverted (project-local loaded then overwritten by global)

3. **src/dgov/router.py**: Fixed `resolve_agent()` to pass `project_root`
   - Was ignoring project-local `agents.toml` due to missing parameter

4. **.dgov/agents.toml**: Rewrote with correct routing
   - Fixed `[routing.qwen-9b]` → `river-9b` (was `river-qwen9`)
   - Fixed `[routing.lt-gov]` → actual agents (was pointing to `supervisor` group)
   - Added abstract role routes: `worker`, `supervisor`, `manager`, `lt-gov`

### Test coverage
- `tests/test_executor_bugs_184_185.py`: 2 regression tests (both pass)
- `tests/test_routing.py`: Routing precedence and role resolution tests
- All executor tests: 47 pass
- All cascade_close tests: 9 pass

## Blockers/debt
- **None** - clean state.

## Next steps
1. Resume normal dgov operations if needed
2. System is ready for new plan submissions

## Notes
- Routing now properly implements policy: project-local > user-global
- Abstract roles (`worker`, `supervisor`, `manager`) resolve correctly
- Escalation chain (worker→supervisor→manager) works via `retry_or_escalate()`
- LT-GOV role (`lt-gov`) available for adversarial audit tasks
