# HANDOVER

## Current State
- Branch: `main` at `718ea74` (clean working tree)
- Tests: targeted unit slices passed; full suite not rerun per project policy
- Panes: none
- Status: `uv run dgov status -r .` reports `0 panes`, `18 healthy / 4 unhealthy` agents, `0` open bugs

## Completed This Session
- **Add live worker trace preview** (`444d5ee`): initial dashboard trace-preview implementation landed in `src/dgov/dashboard.py`.
- **Restore early paste disable before bootstrap** (`4f28288`): moved bracketed-paste disable back in front of long bootstrap pastes in `src/dgov/lifecycle.py`.
- **Normalize terminal control sequences in logs** (`66d7857`): `src/dgov/done.py` now renders backspace/carriage-return/cursor-motion semantics before ANSI stripping so pane output no longer fabricates `ssource` noise.
- **Add trace preview to dashboard output** (`6853167`): finished the dashboard preview path and tests, collapsing the partial trace-preview work into a coherent read-only view.
- **Filter bootstrap noise from pane output** (`718ea74`): `src/dgov/status.py` and `src/dgov/cli/pane.py` now strip internal `dgov-cmd-*.sh` bootstrap echoes and prompt noise from user-facing output/tail paths.
- **Clean stale run state**: closed preserved smoke panes, pruned orphaned worktrees, and cleared stale pane state so `uv run dgov pane list -r .` now reports `No panes.`

## Ledger Snapshot
### Open Bug
- None

### Resolved This Session
- #145 — fixed: lightweight live worker trace preview landed in the dashboard
- #149 — fixed: original bootstrap corruption bug resolved; residual issue is now narrower output formatting noise
- #158 — fixed: plan-run review failure on unused `bootstrap_cmd` local is historical only
- #161 — fixed: stale bug entry claiming fresh panes still reproduced `ssource` was replaced by narrower bug `#163`
- #163 — fixed: user-facing `pane output` no longer shows internal bootstrap command echoes in the repro case

## Key Verification
- `uv run ruff check src/dgov/lifecycle.py`
- `uv run pytest tests/test_lifecycle.py -q -m unit`
- `uv run ruff check src/dgov/done.py src/dgov/dashboard.py tests/test_status.py tests/test_dashboard.py`
- `uv run pytest tests/test_done_strategy.py tests/test_status.py tests/test_dashboard.py -q -m unit`
- `uv run ruff check src/dgov/status.py src/dgov/cli/pane.py tests/test_status.py`
- `uv run pytest tests/test_done_strategy.py tests/test_status.py tests/test_cli_pane.py -q -m unit`
- Manual smoke: fresh `river-35b` pane completed real work and `uv run dgov pane output output-noise-repro --tail 40` returned only `Done.`

## Lookup Cache
- `src/dgov/done.py` — `_strip_ansi()` now renders a small subset of terminal control semantics before stripping escape sequences.
- `src/dgov/dashboard.py` — preview path now uses `_format_trace_data()` first, then falls back to log tail; duplicate trace-preview implementations were removed.
- `src/dgov/status.py` — `tail_worker_log()` and `capture_worker_output()` now pass through `_clean_worker_output_text()` to hide internal bootstrap echoes and prompt noise from user-facing output.
- `src/dgov/cli/pane.py` — `pane output` and `pane tail` follow paths now apply the same output cleaner to streamed log lines.
- `tests/test_status.py` — includes regression coverage for carriage-return, backspace, cursor-rewrite behavior, and internal bootstrap echo filtering.
- `tests/test_dashboard.py` — covers structured trace preview rendering and empty/fallback behavior.
- `src/dgov/lifecycle.py` — early bracketed-paste disable was restored ahead of the first long bootstrap paste.

## Open Issues
- Agent health is still degraded (`18 healthy / 4 unhealthy`). Investigate before leaning on retries/escalation or local tunnel-backed workers.
- Claude-side process docs are still stale in places:
  - `.claude/skills/dgov/SKILL.md`
  - `.claude/commands/dgov-dispatch.md`
  - `.claude/commands/dgov-handover.md`

## Next Steps
- If continuing operator-experience work, build on the dashboard trace preview rather than adding a second live-view surface.
- Update the stale Claude skill/command docs so they match current role-based routing and plan-first governor policy.
