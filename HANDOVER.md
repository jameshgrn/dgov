# HANDOVER

## Current State
- Branch: `main`
- Tests: targeted unit slices passed for the latest routing/retry/output changes; full suite not run per project policy
- Panes: none
- Status: `uv run dgov status -r .` reports `0 panes`, `18 healthy / 4 unhealthy` agents, and `recent failures: 6`
- Ledger: `uv run dgov ledger list -r . -c bug -s open` reports no open bugs

## Completed This Session
- **Stabilize pane retries, routing, and output** (`6fa276e`): reduced worker prompt bloat, preserved retry contract state, fixed stale superseded pane cleanup, added explicit routing degradation behavior, improved live output selection, added `dgov --version`, and tightened the routing/output state model under clanker discipline.
- **Clean stale run state**: closed preserved smoke panes, removed orphaned retry windows, and brought live pane state back to `0 panes`.
- **Refresh CODEBASE map**: regenerated `CODEBASE.md` after the router conflict fix so the module map no longer records a stale parse error.
- **Bug #185 - Fix readonly phase timeout** (`r113-fix-readonly-stall`): Fixed `_dag_wait_any` to detect workers stuck in non-terminal phases (STUCK, IDLE, WAITING_INPUT) and emit `timed_out` after `readonly_timeout` (default 30s). Previously, only terminal phases (DONE, FAILED, UNKNOWN) and the global `max_timeout` were checked, allowing workers to stall indefinitely in readonly phases.

## Ledger Snapshot
### Open Bug
- None

### Accepted Rules In Play
- #154 — accepted: no dual-ownership shims; cut over to one owner in the same change
- #155 — accepted: fix the first wrong layer when the source-of-truth layer is reachable
- #156 — accepted: domain-first placement from `CODEBASE.md`
- #157 — accepted: slow is smooth, smooth is fast

## Key Verification
- `uv run ruff check src/dgov/cli/__init__.py src/dgov/executor.py src/dgov/lifecycle.py src/dgov/persistence.py src/dgov/recovery.py src/dgov/router.py src/dgov/spans.py src/dgov/status.py tests/test_dgov_cli.py tests/test_dgov_panes.py tests/test_lifecycle.py tests/test_router.py tests/test_status.py`
- `uv run pytest tests/test_router.py tests/test_status.py tests/test_dgov_panes.py tests/test_lifecycle.py tests/test_dgov_cli.py -q -m unit`
- `341 passed`
- `dgov --version`
- `uv run dgov status -r .`

## Lookup Cache
- `src/dgov/status.py` — live output now prefers richer Pi transcript tails when tmux/log output is too thin.
- `src/dgov/spans.py` — owns the canonical Pi session/transcript path mapping used by both lifecycle cleanup and status reads.
- `src/dgov/router.py` — degradation attempts now always carry a real backend id; dead partial-failure state is gone.
- `src/dgov/recovery.py` — retry cleanup now deterministically tears down replaced panes instead of leaving idle tmux windows behind.
- `src/dgov/cli/__init__.py` — root CLI now supports `--version`.

## Open Issues
- Agent health is still partially degraded (`18 healthy / 4 unhealthy`), but there are no open repo-tracked bugs at handoff time.
- Dashboard operator UX is the next active work area: default worker visibility and live preview should improve without reintroducing state drift.

## Next Steps
- Make the dashboard default to the live, useful surface: active panes first, richer preview by default, raw tmux only on explicit drill-in.
- Keep using plan-driven dispatch for multi-file operator-surface work.
