# HANDOVER

## Current State
- Branch: `main` at `6853167` (clean working tree)
- Tests: targeted unit slices passed; full suite not rerun per project policy
- Panes: none
- Status: `uv run dgov status -r .` reports `0 panes`, `18 healthy / 4 unhealthy` agents, `1` open bug

## Completed This Session
- **Add live worker trace preview** (`444d5ee`): initial dashboard trace-preview implementation landed in `src/dgov/dashboard.py`.
- **Restore early paste disable before bootstrap** (`4f28288`): moved bracketed-paste disable back in front of long bootstrap pastes in `src/dgov/lifecycle.py`.
- **Normalize terminal control sequences in logs** (`66d7857`): `src/dgov/done.py` now renders backspace/carriage-return/cursor-motion semantics before ANSI stripping so pane output no longer fabricates `ssource` noise.
- **Add trace preview to dashboard output** (`6853167`): finished the dashboard preview path and tests, collapsing the partial trace-preview work into a coherent read-only view.
- **Clean stale run state**: closed preserved smoke panes, pruned orphaned worktrees, and cleared stale pane state so `uv run dgov pane list -r .` now reports `No panes.`

## Ledger Snapshot
### Open Bug
- #163 — `pane output` still shows noisy duplicated command-echo formatting after terminal-control normalization, but fresh panes no longer reproduce the old `ssource` bootstrap corruption

### Resolved This Session
- #145 — fixed: lightweight live worker trace preview landed in the dashboard
- #149 — fixed: original bootstrap corruption bug resolved; residual issue is now narrower output formatting noise
- #158 — fixed: plan-run review failure on unused `bootstrap_cmd` local is historical only
- #161 — fixed: stale bug entry claiming fresh panes still reproduced `ssource` was replaced by narrower bug `#163`

## Key Verification
- `uv run ruff check src/dgov/lifecycle.py`
- `uv run pytest tests/test_lifecycle.py -q -m unit`
- `uv run ruff check src/dgov/done.py src/dgov/dashboard.py tests/test_status.py tests/test_dashboard.py`
- `uv run pytest tests/test_done_strategy.py tests/test_status.py tests/test_dashboard.py -q -m unit`
- Manual smoke: fresh `river-35b` pane completed real work and `uv run dgov pane output ...` no longer showed `ssource`

## Lookup Cache
- `src/dgov/done.py` — `_strip_ansi()` now renders a small subset of terminal control semantics before stripping escape sequences.
- `src/dgov/dashboard.py` — preview path now uses `_format_trace_data()` first, then falls back to log tail; duplicate trace-preview implementations were removed.
- `tests/test_status.py` — includes regression coverage for carriage-return, backspace, and cursor-rewrite behavior in `_strip_ansi()`.
- `tests/test_dashboard.py` — covers structured trace preview rendering and empty/fallback behavior.
- `src/dgov/lifecycle.py` — early bracketed-paste disable was restored ahead of the first long bootstrap paste.

## Open Issues
- Bug `#163` is now the main remaining output-path issue. The catastrophic `ssource` corruption is gone, but `pane output` still prints some noisy duplicated command-echo formatting on fresh panes.
- Agent health is still degraded (`18 healthy / 4 unhealthy`). Investigate before leaning on retries/escalation or local tunnel-backed workers.
- Claude-side process docs are still stale in places:
  - `.claude/skills/dgov/SKILL.md`
  - `.claude/commands/dgov-dispatch.md`
  - `.claude/commands/dgov-handover.md`

## Next Steps
- If continuing reliability work, narrow bug `#163` to the exact remaining command-echo artifacts in `pane output` / log capture.
- If continuing operator-experience work, build on the dashboard trace preview rather than adding a second live-view surface.
- Update the stale Claude skill/command docs so they match current role-based routing and plan-first governor policy.
