# Handover: planning and wait-surface bug fixes landed

## Session context
- Date: 2026-03-29T21:25:03-04:00
- Branch: main @ 6afa75f
- Last commit: Fix wait event payload handling

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| none | closed | No active or preserved panes remain. |

## Open bugs/issues
- None.

## Blockers/debt
- No tracked blockers or open ledger debt.

## Next steps
1. Push `main` to `origin/main` when ready.
2. If another bug-fix pass is needed, start from fresh repros rather than the ledger; there are currently no open bugs tracked.

## Notes
- Landed planning-layer path validation hardening on main:
  `119f244` Reject plan path traversal
- Landed governor wait-surface fix on main:
  `6afa75f` Fix wait event payload handling
- `dgov wait` now reads flattened event payloads returned by `wait_for_events()` and keeps a narrow fallback for legacy `data` blobs.
- `parse_plan_file()` now rejects parent-directory traversal in unit file specs and eval scopes.
- Final status at handoff:
  `dgov status: 0 panes`
  `agents: 15 routed, 8 healthy, 4 routed unhealthy, 3 unprobed, 5 optional unavailable`
  `recent failures: 3`
- Verification run on main for this session:
  `uv run ruff check src/dgov/plan.py tests/test_plan.py`
  `uv run ruff format --check src/dgov/plan.py tests/test_plan.py`
  `uv run pytest tests/test_plan.py -q -m unit -k 'parent_traversal or traversal'`
  `uv run ruff check src/dgov/cli/wait_cmd.py tests/test_dgov_cli.py`
  `uv run ruff format --check src/dgov/cli/wait_cmd.py tests/test_dgov_cli.py`
  `DGOV_SKIP_GOVERNOR_CHECK=1 uv run pytest tests/test_dgov_cli.py -q -m unit -k 'wait_top_level_event_payload or wait_interrupts_top_level_event_payload'`
