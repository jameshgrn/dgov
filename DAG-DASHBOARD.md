# Dashboard Overhaul DAG (v2 — revised after review)

Scope: Phase 0 (noise filter) + Phase 1 (Rich migration) + Phase 2 (SPIM terrain).
Responsive terrain parameters (old T3b) deferred to Phase 3 — requires rewriting the
erosion loop, not a subclass override.

Incorporates findings from REVIEW-DAG-GEMINI.md and REVIEW-DAG-CODEX.md.

## Key changes from v1

1. **Fixed dependency graph**: T0b depends on T0a (erosion.py imports `rich.text.Text`).
2. **Dropped T3b**: Per-region erosion params require modifying `ErosionModel.step()` internals. Deferred.
3. **Removed commit_count/files_changed from worker cards**: Not available from `list_worker_panes()` without live git calls. Detail view already shows these via `review_worker_pane()`.
4. **v2 is opt-in**: `DGOV_DASHBOARD=v2` enables it. v1 remains default until v2 soaks.
5. **Preserved merge/close confirmation**: y/n barrier kept in v2.
6. **Rich markup sanitization**: All user-controlled strings rendered via `Text()` objects, never markup-parsed.
7. **Error handling requirements**: Terrain errors caught with last-good-frame, non-TTY detection, startup failure falls back to v1.
8. **Fixed function references**: T0c targets real functions (`_draw_prompt_preview`, `format_row`).
9. **Test spec expanded**: Covers fallback routing, sanitization, teardown, non-TTY.

## Dependency Graph

```
TIER 0 (parallel — T0a has no deps, T0c has no deps)
  ├─ T0a: Add rich dependency to pyproject.toml
  └─ T0c: Phase 0 noise filter in current dashboard.py

TIER 0.5 (depends on T0a)
  └─ T0b: Vendor SPIM erosion model as src/dgov/terrain.py

TIER 1 (depends on T0a)
  └─ T1a: Create dashboard_v2.py — Rich Layout + Live, worker cards, intent summaries

TIER 2 (depends on T1a + T0b)
  └─ T2a: Integrate terrain sidebar into dashboard_v2

TIER 3 (depends on T1a)
  └─ T3a: Wire CLI to use dashboard_v2 (admin.py)

TIER 4 (depends on T1a, T2a, T3a)
  └─ T4a: Unit tests for dashboard_v2
```

## Task Specifications

### T0a: Add rich dependency
- **Agent**: pi (trivial edit)
- **Escalation**: none needed
- **Files**: pyproject.toml
- **Spec**:
  1. Read pyproject.toml
  2. Add `"rich>=13.0"` to the `dependencies` list, after `click>=8.1`
  3. Run `uv lock`
  4. git add pyproject.toml uv.lock
  5. git commit -m "Add rich>=13.0 dependency"
- **Commit**: "Add rich>=13.0 dependency"

### T0b: Vendor SPIM erosion model
- **Agent**: pi
- **Escalation**: hunter if pi fails
- **Files**: src/dgov/terrain.py (new)
- **Depends on**: T0a (erosion.py imports `rich.text.Text`, so `rich` must be in deps first)
- **Spec**:
  1. Read /Users/jakegearon/projects/writing/src/scilint/tui/erosion.py
  2. Copy it to src/dgov/terrain.py
  3. Change the module docstring to: `"""SPIM erosion terrain model for dgov dashboard."""`
  4. Add attribution comment on line 3: `# Adapted from scilint/tui/erosion.py (stream-power-law incision model)`
  5. Remove any scilint-specific imports (there should be none — it only uses math, random, rich.text)
  6. Do NOT modify any logic — pure copy with docstring change
  7. git add src/dgov/terrain.py
  8. git commit -m "Vendor SPIM erosion model as terrain.py"

### T0c: Phase 0 noise filter
- **Agent**: hunter
- **Escalation**: gemini if hunter fails
- **Files**: src/dgov/dashboard.py
- **Spec**:
  1. Read src/dgov/dashboard.py. Understand the structure:
     - `format_row()` at line 81 formats column values for the table
     - `_draw_pane_row()` at line 399 renders a single table row
     - `_draw_prompt_preview()` at line 443 renders prompt + log tail below the table
     - `_NOISE` set at line 502 inside `_draw_prompt_preview()` filters log tail lines
  2. Expand the `_NOISE` set (line 502) to include these additional patterns:
     ```python
     _NOISE = {
         "unset", "export", "source", "DGOV_", "if DGOV", "set +o",
         "compinit", "zcompdump", "autoload", "kunset",
     }
     ```
     Also add filtering for lines starting with `"^["` or containing `"\x1b"` —
     add these checks in the loop at line 503-513, after the `_NOISE` prefix check:
     ```python
     if stripped.startswith("\x1b") or "\x1b" in stripped:
         continue
     ```
  3. In `format_row()` (line 81), add phase dots to the state display.
     Before the `return` statement, compute phase dots based on state and activity:
     ```python
     # Phase dots indicator
     pane_activity = pane.get("activity", "")
     if pane_state == "active" and "working" in pane_activity:
         dots = "\u2b24\u2b24\u2b24\u25cb\u25cb"
     elif pane_state == "active":
         dots = "\u2b24\u25cb\u25cb\u25cb\u25cb"
     elif pane_state in ("done", "merged"):
         dots = "\u2b24\u2b24\u2b24\u2b24\u2b24"
     elif pane_state in ("failed", "abandoned", "timed_out"):
         dots = "\u2717\u2717\u2717\u2717\u2717"
     elif pane_state == "escalated":
         dots = "\u2b24\u2b24\u25cb\u25cb\u25cb"
     else:
         dots = "\u25cb\u25cb\u25cb\u25cb\u25cb"
     ```
     Prepend dots to the `"state"` value in the returned dict:
     ```python
     "state": truncate(f"{dots} {pane_state}", col_widths["state"] - 2),
     ```
  4. Widen the "state" column from 14 to 20 in the COLUMNS list (line 118)
     to accommodate the phase dots prefix.
  5. Run `uv run ruff check src/dgov/dashboard.py` and `uv run ruff format src/dgov/dashboard.py`
  6. git add src/dgov/dashboard.py
  7. git commit -m "Phase 0: noise filter and phase dots in dashboard"

### T1a: Rich dashboard core (THE BIG ONE)
- **Agent**: gemini (large context needed — must read dashboard.py + status.py + persistence.py)
- **Escalation**: claude (if gemini can't handle the multi-file reasoning)
- **Files**: src/dgov/dashboard_v2.py (new, ~350 lines)
- **Depends on**: T0a (rich must be in deps)
- **Spec**:
  1. Read these files for context:
     - src/dgov/dashboard.py — understand DashboardState, fetch_panes (especially the
       progress.json reading at lines 190-201 and log tail fallback at lines 203-210),
       data_thread, key handling, confirmation flow, `_execute_action`
     - src/dgov/status.py — `list_worker_panes(project_root, session_root, include_freshness=False, include_prompt=False)` for hot-path polling, `tail_worker_log(session_root, slug, lines=N)`
     - src/dgov/persistence.py — `read_events(session_root)` returns list[dict] of event records
  2. Create src/dgov/dashboard_v2.py with:

     **Imports**: `rich.layout.Layout`, `rich.live.Live`, `rich.panel.Panel`,
     `rich.table.Table`, `rich.text.Text`, `rich.console.Console`

     **DashboardState dataclass**: same fields as current dashboard.py plus:
     - `terrain_text: Text | None = None` (rendered terrain, filled by Phase 2)
     - `events: list[dict] = field(default_factory=list)` (last N events)

     **Data thread**: polls at `refresh_interval` (default 1s):
     - Calls `list_worker_panes(project_root, session_root, include_freshness=False, include_prompt=False)`
       NOTE: this calls `_is_done()` on active panes as a side effect — that is expected and acceptable.
     - Reads `progress/<slug>.json` for each pane (same logic as current dashboard.py lines 190-201)
     - Falls back to `tail_worker_log()` for panes without progress activity
     - Calls `read_events(session_root)` and keeps last 8

     **Layout**: Rich Layout with two columns:
     - `worker_panel` (ratio=3): worker table + event feed
     - `terrain_panel` (ratio=1): placeholder Text("[ terrain ]") — Phase 2 fills this

     **Rich Live**: refresh at 4Hz (`refresh_per_second=4`), `auto_refresh=True`

     **Worker table**: Rich Table with columns: Phase, Slug, Agent, State, Activity, Duration
     - Phase column: phase_dots function (same logic as T0c spec above)
     - Activity column: from progress.json message, or activity field from list_worker_panes,
       or state-based fallback ("waiting...", "idle", "exited")
     - Duration column: formatted like current `fmt_duration()`
     - NO commit_count or files_changed — these require live git calls.
       The detail view fetches these on demand via `review_worker_pane()`.

     **CRITICAL — Rich markup sanitization**:
     All user-controlled strings (slug, activity, prompt, event text) MUST be rendered
     using `Text()` objects or `Text.from_ansi()`, NEVER via Rich markup strings.
     This prevents markup injection from progress.json or event payloads.
     Example: `Text(activity_string)` not `f"[bold]{activity_string}[/bold]"`

     **Phase dots function**: `phase_dots(state: str, activity: str) -> str`
     Maps (state, activity) to a 5-dot string. Same mapping as T0c.

     **State color function**: `state_color(state: str) -> str`
     Returns a Rich style string:
     active="yellow", done="green", failed="red", merged="green",
     escalated="magenta", closed="dim", abandoned="red"

     **Event feed**: last 8 events from `read_events()`, rendered as compact lines below the worker table.
     Format: `"HH:MM event_type slug"` — truncated to fit panel width.

     **Key handling**: raw stdin with `select.select()` at 50ms timeout.
     MUST detect non-TTY stdin and skip raw mode if not a TTY (check `sys.stdin.isatty()`).
     Wrap `termios` save/restore in try/finally to guarantee cleanup.
     Keys:
     - q = quit
     - j/k = navigate worker list (update selected index)
     - Enter = detail view (show full pane record as formatted dict via `review_worker_pane()`)
     - m = merge selected (WITH y/n confirmation — prompt in console, not single-keystroke)
     - x = close selected (WITH y/n confirmation)
     - r = force refresh
     - a = attach to selected pane's tmux window

     **Header line**: `"DGOV v{version} | {branch} | {time} | {N} workers"`

     **Footer**: key legend (same keys as above, formatted compactly)

     **Error handling requirements**:
     - If Rich import fails at module level, the module should raise ImportError cleanly
       (this is caught by the CLI fallback in T3a).
     - If stdin is not a TTY, run in display-only mode (no key handling, just Live refresh).
     - If terrain rendering raises, catch the exception, log it, and retain the last good frame.
       Do NOT let a terrain error crash the dashboard.
     - Wrap the entire main loop in try/finally that restores terminal state.

     **Signature**: The module must export:
     ```python
     def run_dashboard_v2(
         project_root: str,
         session_root: str | None = None,
         refresh_interval: float = 1.0,
     ) -> None:
     ```

  3. Do NOT modify any existing files. This is a new file only.
  4. Run `uv run ruff check src/dgov/dashboard_v2.py` and `uv run ruff format src/dgov/dashboard_v2.py`
  5. git add src/dgov/dashboard_v2.py
  6. git commit -m "Rich dashboard v2 with worker cards and intent summaries"

### T2a: Integrate terrain sidebar
- **Agent**: hunter
- **Escalation**: gemini
- **Files**: src/dgov/dashboard_v2.py (edit)
- **Depends on**: T0b (terrain.py exists), T1a (dashboard_v2.py exists)
- **Spec**:
  1. Read src/dgov/terrain.py and src/dgov/dashboard_v2.py
  2. In dashboard_v2.py:
     - Import `ErosionModel` and `render_terrain` from `dgov.terrain`
     - In DashboardState, add: `terrain_model: ErosionModel | None = None`
       Initialize it in `run_dashboard_v2()` with `ErosionModel(width=25, height=30)`
     - In the data thread or render loop, every 3rd tick:
       call `terrain_model.step()` then `render_terrain(terrain_model)` and store as `state.terrain_text`
     - **Wrap terrain step+render in try/except Exception**: on error, log the exception
       and keep `state.terrain_text` unchanged (last good frame). Do NOT crash the dashboard.
     - Replace the terrain placeholder text with the rendered Text in the right panel
     - If terminal width < 100 columns, hide the terrain panel entirely
       (set terrain layout to `visible=False` or use a conditional layout)
  3. Run `uv run ruff check src/dgov/dashboard_v2.py` and `uv run ruff format src/dgov/dashboard_v2.py`
  4. git add src/dgov/dashboard_v2.py
  5. git commit -m "Integrate SPIM terrain sidebar into dashboard v2"

### T3a: Wire CLI to dashboard_v2
- **Agent**: pi
- **Escalation**: hunter
- **Files**: src/dgov/cli/admin.py
- **Depends on**: T1a (dashboard_v2.py exists)
- **Spec**:
  1. Read src/dgov/cli/admin.py. Find the `dashboard` command function at line 216.
  2. Replace the function body with an env var check. v2 is OPT-IN (not default):
     ```python
     if os.environ.get("DGOV_DASHBOARD") == "v2":
         try:
             from dgov.dashboard_v2 import run_dashboard_v2
             run_dashboard_v2(project_root, session_root, refresh)
         except Exception:
             from dgov.dashboard import run_dashboard
             run_dashboard(project_root, session_root, refresh_interval=refresh)
     else:
         from dgov.dashboard import run_dashboard
         run_dashboard(project_root, session_root, refresh_interval=refresh)
     ```
     Note: the `except Exception` (not just `ImportError`) catches Rich import failures,
     terminal setup failures, and any other v2 startup error. The v1 curses dashboard
     is the reliable fallback.
  3. Run `uv run ruff check src/dgov/cli/admin.py` and `uv run ruff format src/dgov/cli/admin.py`
  4. git add src/dgov/cli/admin.py
  5. git commit -m "Wire dashboard v2 into CLI with v1 fallback (opt-in via DGOV_DASHBOARD=v2)"

### T4a: Unit tests
- **Agent**: hunter
- **Escalation**: gemini
- **Files**: tests/test_dashboard_v2.py (new)
- **Depends on**: T1a, T2a, T3a (all code must exist before testing)
- **Spec**:
  1. Read src/dgov/dashboard_v2.py
  2. Create tests/test_dashboard_v2.py with unit tests:

     **Pure function tests** (no mocking needed):
     - `test_phase_dots`: verify phase_dots() for all state/activity combos:
       active+working, active+idle, done, merged, failed, escalated, closed, unknown
     - `test_state_color`: verify state_color() returns correct Rich style for all states
     - `test_dashboard_import`: `from dgov.dashboard_v2 import run_dashboard_v2` succeeds

     **Sanitization tests**:
     - `test_markup_injection`: create a pane dict with `slug="[bold red]evil[/]"` and
       verify the rendered output does not contain Rich markup (the literal brackets
       should appear, not be interpreted as formatting)
     - `test_ansi_in_activity`: create a pane dict with `activity="\x1b[31mred\x1b[0m"`
       and verify ANSI codes are stripped or escaped in rendered output

     **Terrain integration tests**:
     - `test_terrain_import`: `from dgov.terrain import ErosionModel, render_terrain` succeeds
     - `test_terrain_step`: instantiate `ErosionModel(width=10, height=10)`, call `.step()`,
       verify `model.height` grid has changed from initial state
     - `test_terrain_render`: instantiate model, call `render_terrain(model)`,
       verify result is a `rich.text.Text` instance with non-zero length

     **CLI fallback test**:
     - `test_cli_fallback_v1_default`: mock `os.environ` with no `DGOV_DASHBOARD` key,
       verify the `dashboard` command imports from `dgov.dashboard`, not `dgov.dashboard_v2`
     - `test_cli_fallback_v2_opt_in`: mock `os.environ` with `DGOV_DASHBOARD=v2`,
       verify the `dashboard` command attempts to import `dgov.dashboard_v2`

     **Teardown / error handling tests**:
     - `test_terrain_error_resilience`: mock `ErosionModel.step` to raise RuntimeError,
       verify the dashboard data thread does not crash (catches and retains last frame)
     - `test_non_tty_mode`: mock `sys.stdin.isatty()` to return False,
       verify the dashboard can be instantiated without crashing on termios setup

  3. Mock all tmux/subprocess calls. Tests should be pure unit tests.
  4. Add pytest marker: `@pytest.mark.unit` to all tests
  5. Run: `uv run pytest tests/test_dashboard_v2.py -q -m unit`
  6. Run `uv run ruff check tests/test_dashboard_v2.py` and `uv run ruff format tests/test_dashboard_v2.py`
  7. git add tests/test_dashboard_v2.py
  8. git commit -m "Add dashboard v2 unit tests"

## Agent Budget

| Agent  | Tasks          | Cost   |
|--------|----------------|--------|
| pi     | T0a, T0b, T3a  | Free   |
| hunter | T0c, T2a, T4a  | Free   |
| gemini | T1a            | Free   |
| claude | T1a fallback   | Paid   |

Total: 7 tasks, 6 using free models. Claude only if gemini fails on T1a.

## Execution Order

Parallel dispatch where dependencies allow:

```
Step 1 (parallel): T0a + T0c
Step 2 (after T0a): T0b + T1a (parallel — both depend only on T0a)
Step 3 (after T1a): T3a (can merge immediately)
Step 4 (after T0b + T1a): T2a
Step 5 (after T2a + T3a): T4a
```

## Merge Order

1. T0a (pyproject.toml + uv.lock) — no conflicts possible
2. T0c (dashboard.py edits) — no conflicts (only touches dashboard.py)
3. T0b (new file terrain.py) — no conflicts possible
4. T1a (new file dashboard_v2.py) — no conflicts possible
5. T3a (edits admin.py) — depends on T1a merged first
6. T2a (edits dashboard_v2.py) — depends on T0b + T1a merged first
7. T4a (new file test_dashboard_v2.py) — merge last, after all code is on main

## Rollback Plan

If dashboard_v2 is broken after merge:
- v1 is the default. v2 only activates with `DGOV_DASHBOARD=v2`.
- Even if v2 crashes on startup, the `except Exception` in T3a falls back to v1 automatically.
- The old dashboard.py is never modified (except noise filter in T0c, which is independent).
- terrain.py and dashboard_v2.py can be reverted independently.
- To force v1 everywhere: `unset DGOV_DASHBOARD` (it's already the default).
