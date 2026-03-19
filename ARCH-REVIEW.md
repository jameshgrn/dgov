# Architectural Review: Recent Session Changes

**Date:** 2026-03-19
**Reviewer:** Governor (automated audit)
**Scope:** `isometric.py`, `merger.py`, `status.py`, `cli/plan_cmd.py`

---

## 1. Isometric Rendering (`isometric.py`)

### Mathematical Correctness

**Projection Formula:** ✓ **CORRECT**

The implementation uses standard isometric projection with a 2:1 ratio:
```python
sx = cx + (c - r) * (TILE_W // 2)   # Screen X
sy = cy + (c + r) * (TILE_H // 2) - z_off  # Screen Y
```

With `TILE_W=32`, `TILE_H=16`, this gives exactly 2:1 aspect ratio (32:16), which is the classic isometric projection where:
- Horizontal screen offset: `(col - row) × 16`
- Vertical screen offset: `(col + row) × 8`

This correctly maps grid coordinates to isometric screen space.

**Z-Scale Application:** ✓ **CORRECT**

Height values are scaled by `Z_SCALE=32` and subtracted from Y (upward in screen coords):
```python
z_off = int(h_val * Z_SCALE)
sy = cy + (c + r) * (TILE_H // 2) - z_off
```

This is mathematically sound for elevating terrain features.

### Efficiency Issues

**⚠️ CRITICAL: No back-face culling or visibility testing**

The renderer draws ALL tiles regardless of occlusion. For an N×N heightmap:
- Current: O(N²) draw calls, all visible
- Expected: Painter's algorithm requires correct ordering (present), but no early-z or occlusion culling

For large terrains (>100×100), this will be severely slow.

**⚠️ MINOR: Redundant color lookups**

```python
color = PALETTE["bedrock"] if h_val > 0.5 else PALETTE["alluvium"]
```

This check runs per-tile. Consider pre-computing a color map for the height array.

### Robustness Issues

**⚠️ MISSING: Input validation**

No checks for:
- Empty heightmap (`rows=0` or `cols=0`)
- Negative dimensions
- Non-uniform height array rows

**⚠️ FRAGILE: Hard-coded palette values**

Palette colors are magic strings. Should be configurable or loaded from config.

**✅ GOOD: PIL dependency is reasonable**

PIL/Pillow is a standard choice for image generation. Kitty protocol encoding is correct.

---

## 2. Merge System (`merger.py`)

### Mathematical Correctness

**N/A** — This is control flow logic, not mathematical computation.

### Efficiency Issues

**✅ GOOD: Merge lock prevents race conditions**

The `_MergeLock` class using `fcntl.flock` is appropriate for serializing merges.

**⚠️ CONCERN: Multiple sequential git subprocess calls**

In `_plumbing_merge`:
1. `git rev-parse HEAD`
2. `git merge-tree`
3. `git rev-parse branch`
4. `git symbolic-ref HEAD`
5. `git update-ref`
6. `git reset --hard`

Each spawns a new process. For frequent merges, consider batching or using `gitpython` library.

**✅ GOOD: Stash guard minimizes working tree disruption**

The `_stash_guard` context manager properly handles dirty state.

### Robustness Issues

**✅ EXCELLENT: Comprehensive error handling**

Every subprocess call checks `returncode`. Failures return structured `MergeResult` objects.

**✅ EXCELLENT: Protected file restoration**

`_restore_protected_files()` prevents workers from corrupting `CLAUDE.md`, `AGENTS.md`, etc.

**⚠️ RISK: Stash pop failure recovery**

If stash pop fails after successful merge, the warning suggests manual recovery but leaves system in potentially inconsistent state. Consider automatic retry or clearer user guidance.

**✅ GOOD: Conflict detection without side effects**

`_detect_conflicts()` uses `git merge-tree` to predict conflicts before attempting merge.

**⚠️ CONCERN: Auto-rebase fallback logic**

The rebase-on-stale-branch pattern is good, but the fallback to plumbing merge on rebase failure may lose commit history information. Document this behavior clearly.

---

## 3. Status System (`status.py`)

### Mathematical Correctness

**Freshness Algorithm:** ⚠️ **QUESTIONABLE THRESHOLDS**

```python
if overlap and (commits_since > 5 or age_hours > 12):
    freshness = "stale"
elif overlap or commits_since > 0 or age_hours > 4:
    freshness = "warn"
else:
    freshness = "fresh"
```

**Issues:**
- Thresholds (5 commits, 12 hours, 4 hours) are magic numbers with no rationale
- "5 commits since base" as staleness indicator doesn't account for commit size/nature
- File overlap detection is binary; should weight by file importance (e.g., `pyproject.toml` vs. test file)

### Efficiency Issues

**⚠️ PERFORMANCE: Freshness computation is expensive**

Per-pane cost when `include_freshness=True`:
- 2+ git log/diff calls on main repo
- 1+ git diff call on worktree (if alive)
- Up to 3 subprocess calls per pane

For 10 panes: ~30 subprocess calls on each `pane list`.

**✅ GOOD: Optimization flags exist**

`include_freshness=False` and `include_prompt=False` parameters allow callers to skip expensive operations in hot paths.

**✅ GOOD: Log tailing from end**

`tail_worker_log()` seeks from EOF rather than reading entire file. Important for large logs.

### Robustness Issues

**✅ EXCELLENT: Noise filtering**

Comprehensive regex patterns filter agent UI chrome, progress bars, and shell prompts.

**✅ EXCELLENT: Signal extraction**

Pattern matching identifies meaningful actions ("Reading X", "Editing Y", "Testing Z").

**⚠️ CONCERN: Phase computation is heuristic**

`_compute_phase()` relies on string matching against summaries. Will fail for novel workflows or different agent outputs.

**✅ GOOD: Progress JSON reader**

Checks file age (<60s) before reading. Prevents stale progress indicators.

---

## 4. Planning CLI (`cli/plan_cmd.py`)

### Mathematical Correctness

**N/A** — Simple prompt templating.

### Efficiency Issues

**✅ OPTIMAL:** Minimal code, instant execution.

### Robustness Issues

**⚠️ LIMITATION: Very basic refactoring planner**

Only handles move/extract/inline tasks. No support for:
- Multi-file refactors
- Signature changes
- Dependency updates

**⚠️ FRAGILE: Assumes simple src format**

`src.split(":")[0]` works for `path/to/file.py:function` but breaks on Windows paths or complex specifications.

**✅ GOOD: Follows worker prompt pattern**

Numbered steps, explicit file names, commit instructions — matches the documented Qwen worker prompting guidelines.

---

## Summary & Recommendations

| Module | Math | Efficiency | Robustness | Overall |
|--------|------|------------|------------|---------|
| `isometric.py` | ✅ | ⚠️ | ⚠️ | **Needs work** |
| `merger.py` | N/A | ⚠️ | ✅ | **Solid** |
| `status.py` | ⚠️ | ⚠️ | ✅ | **Good** |
| `plan_cmd.py` | N/A | ✅ | ⚠️ | **Minimal but OK** |

### Priority Actions

1. **CRITICAL:** Add input validation to `render_isometric()` — empty/invalid heightmaps will crash
2. **HIGH:** Optimize `status.py` freshness calculation — cache results or reduce git calls
3. **MEDIUM:** Document merge thresholds in `status.py` — explain why 5 commits / 12 hours
4. **MEDIUM:** Add performance benchmarking for isometric renderer at scale (100×100, 500×500)
5. **LOW:** Expand `plan_cmd.py` to handle more refactor types

### Architecture Health

The core merge/status infrastructure is **robust and well-engineered**. The isometric renderer is **mathematically correct but unoptimized** for production use. The planning CLI is **functional but minimal**.

**Overall verdict:** Production-ready for small-scale use; needs optimization before heavy load.
