# Review: Dashboard Overhaul DAG (T0a-T4a)

**Reviewer:** Gemini CLI
**Date:** Monday, March 16, 2026
**Status:** Partial (5 READY, 3 NEEDS_EDIT)

## Executive Summary
The DAG is well-structured and provides clear separation between dependencies (Rich/SPIM) and the core implementation (Dashboard V2). However, there are significant API assumption errors in T1a regarding the availability of commit counts and file changes in the persistent pane records. Additionally, some UI logic currently residing in `dashboard.py` (like `progress.json` reading) should be moved to the shared `status.py` API to ensure T1a remains lean.

---

## 1. Task Reviews

### T0a: Add rich dependency
- **Completeness:** High. Explicitly mentions `pyproject.toml` and `uv lock`.
- **Dependency Correctness:** Correct (Tier 0).
- **Risk Assessment:** Low. Trivial edit.
- **Verdict:** **READY**

### T0b: Vendor SPIM erosion model
- **Completeness:** High. Clear instructions for docstring changes and attributions.
- **Dependency Correctness:** Correct (Tier 0).
- **Risk Assessment:** Low, provided the source path `/Users/jakegearon/projects/writing/src/scilint/tui/erosion.py` is accessible to the worker. *Note: The worker may need `cat` if `read_file` workspace restrictions apply.*
- **Verdict:** **READY**

### T0c: Phase 0 noise filter
- **Completeness:** Good, but contains a naming ambiguity.
- **Risk Assessment:** The spec refers to `_draw_panes`, but the current `src/dgov/dashboard.py` uses `_draw_pane_row` and `format_row`.
- **Conflict Potential:** Low.
- **API Assumptions:** Mentions "replace raw `last_output` display with the `activity` field". `last_output` is a field returned by `list_worker_panes` but is currently not displayed in the table columns. The spec should clarify if this refers to the table or the preview pane.
- **Verdict:** **NEEDS_EDIT** (Clarify function names and `last_output` display location).

### T1a: Rich dashboard core (The Big One)
- **Completeness:** Very high detail on layout and features.
- **Dependency Correctness:** Depends on T0a.
- **Risk Assessment:** **High.** This task combines complex Rich Layout logic with keyboard polling and multi-file data fetching.
- **API Assumptions:** **FAIL.**
    1. The spec requires "Line 3: commit count + files changed (from pane record, NOT live git calls)". **Finding:** `src/dgov/persistence.py`'s `WorkerPane` and the `panes` table do **not** store `commit_count` or `files_changed`.
    2. `src/dgov/status.py` computes these fields live via `_compute_freshness` only if `include_freshness=True`.
    3. Running `include_freshness=True` at 1Hz is too expensive (3 git calls per pane per second).
- **Missing Task:** A task is needed to update `persistence.py` to store these metrics or update `status.py` to cache them.
- **Verdict:** **NEEDS_EDIT** (Address the missing DB fields for commit/file stats).

### T2a: Integrate terrain sidebar
- **Completeness:** High. Clear import and integration steps.
- **Dependency Correctness:** Correct (Depends on T0b, T1a).
- **Risk Assessment:** Medium. Half-block doubling logic for height is clever but needs careful implementation.
- **Verdict:** **READY**

### T3a: Wire CLI to dashboard_v2
- **Completeness:** High. Includes a perfect code snippet for `src/dgov/cli/admin.py`.
- **Dependency Correctness:** Correct (Depends on T1a).
- **Verdict:** **READY**

### T3b: Responsive terrain parameters
- **Completeness:** Clear logic for `DgovErosionModel` subclass.
- **Dependency Correctness:** Correct (Depends on T0b).
- **Verdict:** **READY**

### T4a: Integration tests
- **Completeness:** Good list of unit test cases.
- **Dependency Correctness:** Correct (Tier 4).
- **Verdict:** **READY**

---

## 2. Global Recommendations

### Move `progress.json` logic to `status.py`
Currently, `src/dgov/dashboard.py` (L147-L162) contains logic to read `.dgov/progress/*.json` files to update the `activity` field. `status.py`'s `list_worker_panes` does not do this.
- **Recommendation:** Move this logic into `src/dgov/status.py` so that both the old dashboard and the new Rich dashboard benefit from the same "intent summary" data without duplicating code.

### ANSI Stripping Consolidation
`src/dgov/status.py` and `src/dgov/dashboard.py` both define `_ANSI_RE` and `_strip_ansi`.
- **Recommendation:** T0c should move these to `src/dgov/art.py` as part of the "noise filter" task.

### Commit Count Persistence
If the Rich dashboard is to show commit counts without live git calls, `src/dgov/lifecycle.py` or the `review-fix` logic should update the pane's metadata in the DB with these counts upon significant events.

---

## 3. Verdict Summary

| Task | Verdict | Reason |
|------|---------|--------|
| T0a  | READY   | |
| T0b  | READY   | |
| T0c  | NEEDS_EDIT | Ambiguous function names and display logic. |
| T1a  | NEEDS_EDIT | **Blocked** on missing DB fields for commit/file stats. |
| T2a  | READY   | |
| T3a  | READY   | |
| T3b  | READY   | |
| T4a  | READY   | |
