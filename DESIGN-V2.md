# dgov v2 Unified Design: Dashboard + Lieutenant Governor Hierarchy

## Status: SIGNED OFF — implementation starting

### Resolved decisions (2026-03-16)
- **Rich**: Just add it. No fallback dance. 4 packages.
- **Merge concurrency**: MergeQueue in state.db. `BEGIN IMMEDIATE` for serialization. LT-GOVs enqueue, governor processes serially.
- **Project root**: `DGOV_PROJECT_ROOT` env var injected into LT-GOV panes. CLI default chain: `--project-root` flag → env var → cwd.
- **Pre-merge hook**: Non-issue. `_plumbing_merge` uses `commit-tree`/`update-ref` (plumbing), not porcelain `git merge`. Hooks don't fire.
- **Phase order**: Swapped — LT-GOV is Phase 2 (the unlock), terrain is Phase 3 (eye candy).
- **Terrain sidebar**: Off by default, `t` to toggle.
- **LT-GOV failure**: Option (c) — orphans keep running, governor triages manually.
- **ProgressPacket author**: Option (c) — emit from `_is_done`, zero-cost.
- **Merge processor**: Governor poll loop. No new panes, no new coordination.
- **Advisory visibility**: Shared `.dgov/advisories/` in project root, readable by all workers.

---

## 1. How the Dashboard Visualizes the Hierarchy

### Layout Architecture

Rich `Layout` splits the terminal into named regions. The dashboard has two modes that share the same rendering pipeline — the only difference is how the worker list is grouped.

```
┌─────────────────────────────────────────────────────────────────────┐
│  DGOV v0.9.0 │ main │ 14:32:07 │ 6 workers │ 2 pending merges     │
├──────────────────────────────────────────┬──────────────────────────┤
│                                          │                          │
│  WORKER PANEL (scrollable)               │  TERRAIN SIDEBAR         │
│                                          │  (SPIM erosion model)    │
│  ┌── tier-1 (lt-gov: claude) ──────────┐ │                          │
│  │ ⬤⬤⬤⬤○ fix-parser     cc  4m12s    │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │         Reading src/dgov/parser.py  │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │         +42 -8 across 2 files       │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │                                     │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │ ⬤⬤○○○ add-metrics    pi  1m30s    │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │         Working on metrics.py       │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │         0 commits so far            │ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  └─────────────────────────────────────┘ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│                                          │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  ┌── tier-2 (lt-gov: gemini) ──────────┐ │  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  │
│  │ ⬤⬤⬤⬤⬤ refactor-cli   gm  8m44s  │ │                          │
│  │         ✓ merged                    │ │                          │
│  └─────────────────────────────────────┘ │                          │
│                                          │                          │
├──────────────────────────────────────────┴──────────────────────────┤
│ q:quit  j/k:↑↓  Tab:tier  Enter:detail  d:diff  m:merge  x:close  │
└─────────────────────────────────────────────────────────────────────┘
```

### Flat Mode (backwards compatible, no LT-GOVs)

When no LT-GOV panes exist, the worker panel is a flat list of 3-line cards. Identical to the hierarchical layout minus the tier headers. This is the Phase 1 target.

```
│  ⬤⬤⬤⬤○ fix-parser     cc  4m12s                                │
│           Reading src/dgov/parser.py                               │
│           +42 -8 across 2 files                                    │
│                                                                    │
│  ⬤⬤○○○ add-metrics    pi  1m30s                                 │
│           Working on metrics.py                                    │
│           0 commits so far                                         │
```

### Hierarchical Mode (with LT-GOVs)

Workers are grouped under their parent LT-GOV. Each tier has a collapsible header showing:
- LT-GOV slug, agent, and state
- Tier summary: `3/5 done, 1 blocked, ETA ~4m`
- Collapse/expand with `Tab` or `Enter` on header

Detection: a pane with `metadata.role = "lt-gov"` and `metadata.tier_name` is a LT-GOV. Workers with `metadata.parent_ltgov = <slug>` are grouped under it. Ungrouped workers show in a "direct" section at the top.

### 3-Line Worker Cards

Each card shows exactly three lines — no more, no less. This is the atomic visual unit.

```
Line 1: phase_dots  slug          agent_label  duration
Line 2:             intent_summary (from progress.json or log heuristic)
Line 3:             commit_delta ("+N -M across K files" or "0 commits")
```

**Phase dots** encode five stages: `⬤⬤⬤○○`

| Phase | Meaning | Detection |
|-------|---------|-----------|
| 1 bootstrap | Shell setup, env exports | `current_command in (zsh, bash)` and `duration < 30s` |
| 2 reading | Agent reading files | progress.json `status = "reading"` or log contains "Reading" |
| 3 working | Active editing/coding | progress.json `status = "working"` or commits appearing |
| 4 testing | Running tests/lint | log contains `pytest`, `ruff`, `npm test` etc. |
| 5 committing | Final commit + cleanup | done signal present or `state = "done"` |

Phase detection is best-effort heuristic. The progress.json path (already written by some agents) is authoritative when present. Log tail keyword matching is the fallback.

### Communication Flow Visualization

Not worth visualizing as animated arrows or packet streams — that's noise. Instead, the tier header line acts as the summary channel:

```
┌── tier-1 (claude) ── 3/5 done │ 1 escalation absorbed │ ETA ~4m ──┐
```

The key information from TierSummaryPackets is compressed into that single header line. Escalation events appear as brief flash highlights (Rich `Style` with 1-second TTL) on the tier header.


## 2. Minimum Viable LT-GOV

### The insight: a LT-GOV is just a governor-mode claude in a worktree

The minimum viable LT-GOV requires **zero new primitives**. It's a claude worker pane whose prompt is a meta-prompt telling it to act as a sub-governor using existing `dgov` CLI commands.

### Implementation

```python
# Governor dispatches a LT-GOV:
dgov pane create -a claude -s ltgov-tier1 -m bypassPermissions -p "
You are a lieutenant governor managing a tier of workers.
Your project root is: /path/to/project

## Your workers:
1. fix-parser: 'Fix the parser bug in src/dgov/parser.py'
2. add-metrics: 'Add per-agent success rate to metrics.py'
3. update-tests: 'Add tests for the new metrics functions'

## Your job:
For each worker:
  1. dgov pane create -a pi -p '<worker prompt>' -r /path/to/project
  2. dgov pane wait <slug>
  3. dgov pane review <slug>
  4. If review passes: dgov pane merge <slug>
  5. If review fails: dgov pane close <slug>, then retry with better prompt

When ALL workers are merged, write a summary to .dgov/progress/ltgov-tier1.json:
{\"status\": \"done\", \"merged\": [...slugs...], \"failed\": [...slugs...]}

Then exit.

## Rules:
- Never push to remote
- Never edit files directly — only dispatch workers
- If a worker is stuck for >5 minutes, close and retry with claude instead of pi
- If you encounter a structural problem (wrong architecture, conflicting files),
  write it to .dgov/progress/ltgov-tier1.json with status: 'escalation' and exit
"
```

### What makes this work today

1. **dgov CLI is available in worktrees** — workers can run `dgov` commands against the same project root
2. **SQLite WAL mode** — multiple processes can read/write the state DB concurrently
3. **progress.json** — the LT-GOV writes its tier summary there, dashboard reads it
4. **Event system** — all sub-worker events (created, done, merged) are visible to the governor

### What's missing (and acceptable for MVP)

- **No formal parent-child relationship in DB** — the governor just knows which slugs it dispatched to each LT-GOV via the prompt. The LT-GOV slug prefix provides implicit grouping (e.g., all workers created by `ltgov-tier1` will have slugs like `fix-parser`, `add-metrics`).
- **No structured reporting format** — the LT-GOV writes free-form JSON to progress.json. Good enough; the governor reads it.
- **No automatic tier detection in dashboard** — Phase 3 MVP shows LT-GOVs as regular workers with a `[LT-GOV]` prefix in their activity. Phase 5 adds real grouping.

### MVP scope

One new field in pane metadata: `role` (values: `"worker"` default, `"lt-gov"`). Set via `--role lt-gov` flag on `dgov pane create`, stored in the metadata JSON column. No schema migration needed — metadata is already a JSON blob.

The dashboard checks `metadata.role` and renders LT-GOV panes differently (bold, tier header style) even before full hierarchy support.


## 3. Communication Primitives Mapped to Existing State

### Mapping table

| Packet Type | Storage | Format | New? |
|-------------|---------|--------|------|
| **TaskPacket** (gov → worker) | `panes.prompt` + `panes.metadata` | Already exists: prompt text + metadata JSON | No — add `complexity`, `scope_files` to metadata |
| **ProgressPacket** (worker → lt-gov) | `.dgov/progress/<slug>.json` | Already exists: `{turn, message, status}` | Extend: add `phase`, `confidence`, `files_touched` |
| **EscalationPacket** (worker → lt-gov) | `events` table | `pane_blocked` event with `data` JSON | No — already exists. Add `partial_progress` to data |
| **AdvisoryPacket** (worker → scratchpad) | `.dgov/advisories/<slug>.json` | New file | **Yes** — new file-based channel |
| **TierSummaryPacket** (lt-gov → gov) | `.dgov/progress/<ltgov-slug>.json` | Extension of progress.json | No — same file, richer schema |
| **AttentionPacket** (gov → worker) | `.dgov/attention/<slug>.json` | New file | **Yes** — new file-based channel |
| **ConflictPacket** (merge → lt-gov) | `events` table | `pane_merge_failed` event | No — already exists |

### What's new DB schema

**Nothing.** All new packet types map to either:
1. Existing DB columns (prompt, metadata JSON, events table)
2. File-based channels (`.dgov/progress/`, `.dgov/advisories/`, `.dgov/attention/`)

File-based channels are the right choice because:
- Workers in worktrees can write them without DB contention
- Dashboard polls them at refresh interval (already does this for progress.json)
- No migration required
- Human-readable for debugging

### Extended progress.json schema (Phase 4)

```json
{
  "turn": 12,
  "message": "Editing src/dgov/parser.py",
  "status": "working",
  "phase": 3,
  "phase_label": "working",
  "confidence": 0.8,
  "files_touched": ["src/dgov/parser.py", "tests/test_parser.py"],
  "commits": 2,
  "lines_added": 42,
  "lines_removed": 8,
  "hypothesis": "Parser fails on nested brackets",
  "evidence": "Added test case that reproduces the bug",
  "blockers": []
}
```

### Advisory channel (Phase 4)

Workers write observations that might help other workers. The LT-GOV (or governor) can relay these.

```json
// .dgov/advisories/fix-parser.json
{
  "ts": "2026-03-16T14:32:07Z",
  "observations": [
    {
      "type": "file_structure",
      "message": "parser.py imports from legacy module that was renamed",
      "files": ["src/dgov/parser.py"],
      "relevance": ["refactor-imports"]
    }
  ]
}
```

### Attention channel (Phase 5)

Governor sends salience hints to workers. Workers read on startup and periodically.

```json
// .dgov/attention/fix-parser.json
{
  "hot_files": ["src/dgov/parser.py", "src/dgov/persistence.py"],
  "avoid_files": ["src/dgov/dashboard.py"],
  "budget_hint": "5 minutes remaining",
  "priority": "high"
}
```


## 4. SPIM Terrain as Information Display

### Core mapping: system state → erosion parameters

The ErosionModel has per-cell control over three things: height (elevation), K (erosion coefficient), and uplift. The terrain grid is partitioned into regions, one per active worker/LT-GOV.

#### Region assignment

The terrain grid (width x height) is divided into rectangular regions using a simple packing algorithm:

```
N = number of active panes
cols_per_region = terrain_width // ceil(sqrt(N))
rows_per_region = terrain_height // ceil(N / ceil(sqrt(N)))
```

Each region is a drainage basin. The boundary cells between regions are fixed at 0 (drain cells), creating natural watershed divides.

#### Parameter mapping

| System State | Terrain Parameter | Effect |
|---|---|---|
| Worker created | Region initialized with high uplift | Mountains rise in that basin |
| Worker velocity (commits/min) | K (erosion coefficient) for region | Fast workers erode deeper channels |
| Worker phase (1-5) | Elevation bias | Phase 1 = high plateau, Phase 5 = eroded lowland |
| Worker stuck/blocked | K → 0, uplift stays high | Mountains grow with no drainage — red/hot |
| Worker done/merged | Uplift → 0, K stays high | Terrain erodes to flat lowland |
| Merge flow | River threshold lowered for merged regions | Blue rivers appear in completed basins |
| Escalation | Sudden K spike + color shift | "Avulsion" — channel reorganization |

#### Implementation approach

Subclass `ErosionModel` as `DgovErosionModel` that accepts a list of worker states and maps them to per-cell parameters:

```python
class DgovErosionModel(ErosionModel):
    """SPIM terrain driven by dgov worker state."""

    def __init__(self, width: int, height: int, seed: int | None = None):
        super().__init__(width=width, height=height, seed=seed)
        self.regions: dict[str, tuple[int, int, int, int]] = {}  # slug → (r0, c0, r1, c1)
        self._region_K: dict[str, float] = {}
        self._region_uplift: dict[str, float] = {}

    def update_from_panes(self, panes: list[dict]) -> None:
        """Recompute region assignments and per-cell parameters from pane state."""
        self._assign_regions(panes)
        for slug, (r0, c0, r1, c1) in self.regions.items():
            pane = next((p for p in panes if p["slug"] == slug), None)
            if not pane:
                continue
            K, uplift = self._params_for_pane(pane)
            # Apply to all cells in region
            for r in range(r0, r1):
                for c in range(c0, c1):
                    # Store per-cell K and uplift (need to extend base model)
                    pass

    def _params_for_pane(self, pane: dict) -> tuple[float, float]:
        state = pane.get("state", "active")
        phase = pane.get("phase", 3)
        if state in ("done", "merged"):
            return (0.005, 0.0)        # high erosion, no uplift → flatten
        if state in ("failed", "abandoned"):
            return (0.0, 0.002)         # no erosion, uplift → growing mountains (stuck)
        if state == "active":
            velocity = pane.get("commits_per_min", 0)
            K = 0.001 + 0.004 * min(velocity, 1.0)  # faster → more erosion
            uplift = 0.002 * (1.0 - phase / 5.0)     # earlier phases → more uplift
            return (K, uplift)
        return (0.003, 0.001)  # default
```

#### Color overlay for stuck workers

The `_elevation_color` function in the existing erosion.py uses elevation bands for color. For dgov, add a region-aware color override:

- Active workers: standard terrain palette (green slopes, tan ridges, snow peaks)
- Stuck workers (>5min no commits, active state): shift hue toward red/orange
- Done workers: shift toward cool blue-green (lowland palette)
- LT-GOV regions: slightly brighter/more saturated than worker regions

#### River = merge flow

Rivers (cells where `area[r][c] > threshold`) naturally form where erosion carves channels. In the dgov terrain:

- Completed workers have low elevation → water flows through them
- The "main channel" (governor merge path) is the lowest point, at the terrain boundary
- Multiple merged workers create converging tributaries → visible merge topology

This happens naturally from the erosion model. No special rendering needed — just set the right K/uplift values and the physics does the rest.

#### Practical sidebar sizing

The terrain sidebar should be ~25-30 columns wide. At 30 cols wide and ~20 terminal rows, that's a 30x40 grid (half-block doubles vertical resolution). Enough for 4-6 visible drainage basins.

For terminals narrower than 100 columns, hide the sidebar entirely and show a 1-line terrain "sparkline" in the header instead (elevation profile as braille characters).


## 5. Implementation Phases

### Phase 0: Curses dashboard stabilization [DONE]

**Status**: Complete. `dashboard_v2.py` (484 lines, curses) ships phase dots (`⬤/○`), log tail activity, keybindings (a/s/d/c/m/x). All bugs fixed.

### Phase 1: Rich migration with intent summaries

**Goal**: Replace curses with Rich Layout + Live. 3-line worker cards. No hierarchy yet.

**Tasks**:
1. Add `rich>=13.0` to `pyproject.toml` dependencies
2. Create `src/dgov/dashboard_v2.py` — new Rich-based dashboard
   - `Layout` with two columns: worker panel (ratio=3) + terrain sidebar (ratio=1)
   - `Live` display with configurable refresh rate
   - Worker cards as `Panel` objects inside a vertical group
   - Phase dots, intent summaries, commit deltas per card
   - Same keybindings as current dashboard (vim-style navigation)
   - Detail view as overlay `Panel`
3. Wire `dgov` CLI to use dashboard_v2 (feature flag: `DGOV_DASHBOARD=v2` env var, default to v2)
4. Keep `dashboard.py` as fallback for `DGOV_DASHBOARD=v1` or `TERM=dumb`

**Files changed**:
- `/Users/jakegearon/projects/dgov/pyproject.toml` (add rich dep)
- `/Users/jakegearon/projects/dgov/src/dgov/dashboard_v2.py` (new, ~400 lines)
- `/Users/jakegearon/projects/dgov/src/dgov/cli/admin.py` (wire new dashboard)

**Effort**: Multi-file claude task. Rich Layout + Live integration requires understanding the existing data flow (DashboardState, fetch_panes, data_thread) and reimplementing the rendering layer.
**Unlocks**: 24-bit color, proper layout, foundation for terrain sidebar

### Phase 2: LT-GOV MVP — the unlock

**Goal**: Governor can dispatch a LT-GOV that manages its own tier of workers. Requires merge queue for concurrency safety.

**Prerequisites (implement first)**:
1. **merge_queue table in state.db** — serializes all merges to main
   ```sql
   CREATE TABLE IF NOT EXISTS merge_queue (
       ticket TEXT PRIMARY KEY,
       branch TEXT NOT NULL,
       requester TEXT NOT NULL,
       status TEXT DEFAULT 'pending',
       result TEXT,
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
       processed_at TIMESTAMP
   );
   ```
2. **enqueue_merge / process_next** in persistence.py — `BEGIN IMMEDIATE` for serialization, merge outside transaction
3. **DGOV_PROJECT_ROOT env var** — CLI default chain: `--project-root` flag → `DGOV_PROJECT_ROOT` → cwd
4. **Merge processor in governor loop** — periodically calls `process_next()`, emits events on completion

**LT-GOV tasks**:
1. Add `--role` option to `dgov pane create` (values: `worker`, `lt-gov`)
   - Stored in metadata JSON, no schema change
2. Add `--parent` option to `dgov pane create` for workers dispatched by a LT-GOV
   - Stored in metadata as `parent_ltgov`
3. Create meta-prompt template for LT-GOVs in `src/dgov/templates.py`
   - Template takes: tier name, worker task list, policy (retry/escalate thresholds)
   - Injects `DGOV_PROJECT_ROOT` so LT-GOV always targets main repo
   - Outputs a structured prompt that tells the LT-GOV agent how to use `dgov` commands
4. Add `dgov tier create -a claude -n "tier-1" --tasks tasks.json` convenience command
   - Reads a JSON/TOML file of worker tasks
   - Dispatches a LT-GOV with the meta-prompt
5. Dashboard shows LT-GOV panes with `[LT-GOV]` prefix and bold styling (flat mode still)
6. LT-GOVs call `dgov merge enqueue <slug>` instead of `dgov pane merge <slug>` — governor processes queue

**Files changed**:
- `/Users/jakegearon/projects/dgov/src/dgov/persistence.py` (merge_queue table + enqueue/process)
- `/Users/jakegearon/projects/dgov/src/dgov/cli/pane.py` (--role, --parent flags, DGOV_PROJECT_ROOT default)
- `/Users/jakegearon/projects/dgov/src/dgov/lifecycle.py` (pass role/parent to metadata, inject env vars for LT-GOV)
- `/Users/jakegearon/projects/dgov/src/dgov/templates.py` (LT-GOV meta-prompt template)
- `/Users/jakegearon/projects/dgov/src/dgov/cli/__init__.py` (tier command group, merge enqueue/process commands)
- `/Users/jakegearon/projects/dgov/src/dgov/dashboard_v2.py` (LT-GOV card styling)

**Effort**: Multi-file, multi-worker. Split: (a) merge queue infra, (b) env var plumbing, (c) LT-GOV meta-prompt + tier CLI, (d) dashboard styling.
**Unlocks**: Hierarchical delegation. Governor can manage 10+ workers through 2-3 LT-GOVs.

### Phase 3: SPIM terrain sidebar — eye candy

**Goal**: Live erosion terrain in the sidebar, driven by worker state.

**Tasks**:
1. Copy `erosion.py` from scilint into dgov as `src/dgov/terrain.py`
   - Strip scilint-specific imports
   - Subclass as `DgovErosionModel` with region assignment and state-driven parameters
2. Add `render_terrain()` call to dashboard_v2's refresh loop
   - Terrain updates at ~5 FPS (every 3rd dashboard refresh tick)
   - `update_from_panes()` called on each data refresh (every 1s)
3. Responsive terrain: hide sidebar below 100 cols, show sparkline in header instead
4. Add color overlays for stuck workers (red shift) and completed workers (blue shift)
5. Off by default, `t` key to toggle

**Files changed**:
- `/Users/jakegearon/projects/dgov/src/dgov/terrain.py` (new, ~200 lines — ErosionModel fork + DgovErosionModel)
- `/Users/jakegearon/projects/dgov/src/dgov/dashboard_v2.py` (integrate terrain panel)

**Effort**: 1 claude task (multi-file but focused — port erosion model + wire to dashboard)
**Unlocks**: The marquee visual feature. Geomorphological metaphor becomes literal.

### Phase 4: Structured communication primitives

**Goal**: Workers and LT-GOVs communicate via typed packets instead of free-form files.

**Tasks**:
1. Define packet dataclasses in `src/dgov/packets.py`
   - `ProgressPacket`, `EscalationPacket`, `AdvisoryPacket`, `TierSummaryPacket`, `AttentionPacket`
   - Each has `to_json()` / `from_json()` methods
   - Each writes to its designated file path under `.dgov/`
2. Add packet writer helpers that workers call (via shell commands or agent tool use)
   - `dgov progress --slug X --phase 3 --confidence 0.8 --message "Editing parser.py"`
   - `dgov advisory --slug X --type file_structure --message "..."`
3. Add packet reader to dashboard data thread
   - Read all packet files on each refresh
   - Aggregate into per-tier summaries
4. Extend event system with packet-derived events
   - `progress_updated`, `advisory_posted`, `attention_sent`

**Files changed**:
- `/Users/jakegearon/projects/dgov/src/dgov/packets.py` (new, ~150 lines)
- `/Users/jakegearon/projects/dgov/src/dgov/cli/pane.py` (progress/advisory subcommands)
- `/Users/jakegearon/projects/dgov/src/dgov/persistence.py` (new event types)
- `/Users/jakegearon/projects/dgov/src/dgov/dashboard_v2.py` (read packets in data thread)

**Effort**: Multi-file claude task. Straightforward dataclass + file I/O work.
**Unlocks**: Structured data flow. Dashboard shows real phase indicators instead of heuristic guesses. LT-GOVs get typed tier summaries.

### Phase 5: Hierarchical dashboard

**Goal**: Dashboard groups workers under their LT-GOV tiers.

**Tasks**:
1. Tier detection in data thread: group panes by `metadata.parent_ltgov`
2. Tier header rendering: collapsible group with summary line
3. Nested navigation: `Tab` to switch between tiers, `j/k` within tier
4. Terrain regions mapped to tiers (not individual workers) — cleaner visual
5. Tier-level actions: merge all in tier, close tier, retry tier

**Files changed**:
- `/Users/jakegearon/projects/dgov/src/dgov/dashboard_v2.py` (major refactor of worker panel)
- `/Users/jakegearon/projects/dgov/src/dgov/terrain.py` (tier-level region assignment)

**Effort**: Multi-file claude task. The navigation model is the complex part — nested scrolling with collapse/expand.
**Unlocks**: Full hierarchical visibility. Governor sees tiers, not a flat list of 15 workers.


## 6. Design Questions

### Resolved

1. ~~**Rich as a runtime dependency?**~~ **Yes.** Just add `rich>=13.0`. 4 packages (rich, markdown-it-py, mdurl, pygments). No fallback dance.

2. ~~**Terrain sidebar default on or off?**~~ **Off by default**, `t` to toggle. Auto-hide below 100 cols.

3. ~~**LT-GOV agent choice.**~~ **claude only** for MVP. Gemini can't reliably do multi-step CLI workflows.

4. ~~**LT-GOV concurrency limit.**~~ Default `max_concurrent: 3` for LT-GOVs, configurable in `config.toml`.

5. ~~**LT-GOV failure mode.**~~ **Option (c)** — orphaned workers keep running, governor triages manually. Phase 5 can add auto-adoption.

6. ~~**Who writes ProgressPackets?**~~ **Option (c)** — emit from `_is_done` as side effect. Zero-cost bootstrap. Phase 4 adds agent-native writing.

7. ~~**Advisory visibility.**~~ **Shared** `.dgov/advisories/` in project root, readable by all workers.

8. ~~**Merge concurrency.**~~ **MergeQueue in state.db.** `BEGIN IMMEDIATE` serialization. LT-GOVs enqueue, governor poll loop processes.

9. ~~**Merge processor.**~~ **Governor poll loop** (option a). `process_next()` called periodically. No new panes.

10. ~~**Project root in worktrees.**~~ **`DGOV_PROJECT_ROOT` env var** injected into LT-GOV panes. CLI default chain: flag → env → cwd.

### Open (nice to have, not blocking)

11. **Terrain seed determinism.** Deterministic (from session ID) or random? Leaning random — simpler, more varied.

12. **Dashboard standalone mode.** Run from any terminal via SQLite DB? Low priority but architecturally easy if data layer stays clean.

13. **Color theme.** Earth tones (visual cohesion with terrain) vs existing curses palette (yellow/green/red/cyan)? Leaning earth tones.
