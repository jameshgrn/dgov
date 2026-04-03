# Feature Module Review

## Summary

The codebase is largely well-structured with most modules serving clear purposes and being actively imported. The main concerns are: (1) `models.py` is a 3-line stub containing only a re-export from `inspection.py` — this adds an unnecessary indirection layer, (2) `strategy.py` appears to be an unfinished/incomplete module with only 2 simple helper functions that could live elsewhere, and (3) `retry.py` and `recovery.py` have overlapping retry logic that could confuse developers about where retry behavior lives.

## Findings by file

### dag.py
- No significant dead code or orphaned features. This module is actively used by the CLI and orchestrator.
- Contains substantial DAG execution logic (~543 lines) — consider splitting persistence concerns into a separate module if it grows further.

### monitor.py
- No significant issues. Well-integrated with the event system and persistence layer.
- Active monitoring module used throughout the codebase.

### monitor_hooks.py
- No significant issues. Clean small module (85 lines) with a single `MonitorHook` dataclass.

### dashboard_v2.py
- [LINE:1] OVERLAP: `dashboard_v2.py` appears to be an older or alternative version alongside `dashboard.py` (which is 846 lines vs 80 lines). The v2 file is much smaller and likely incomplete or deprecated.

### agents.py
- No significant issues. Core module for agent management and communication.

### recovery.py
- [LINE:1-250] OVERLAP: Retry/escalation logic overlaps with `retry.py`. Both handle retry counting and policies — `recovery.py` handles the actual pane operations (retry_worker_pane, escalate_worker_pane) while `retry.py` handles retry policies and context. The split is somewhat arbitrary.

### inspection.py
- No significant issues. Exports `review_worker_pane`, `diff_worker_pane`, `rebase_governor` which are all actively used.

### yapper.py
- No significant issues. Core dispatch mechanism used throughout.

### batch.py
- No significant issues. Batch dispatch functionality is actively used.

### preflight.py
- No significant issues. Validation logic for pane creation.

### experiment.py
- No significant issues. Used for running experiment loops and logging.

### strategy.py
- [LINE:1] ORPHANED: Only contains `build_prompt_dict` (70 lines) and `load_prompt_from_env` (33 lines). These are simple helper functions that could live in a utils module or alongside the code that uses them.
- Only imported in `strategy.py` itself — minimal external usage suggests this could be consolidated.

### models.py
- [LINE:1-10] ORPHANED/REDUNDANT: This is a 3-line file that re-exports `MergeResult` from `inspection.py`. The comment says "extracted from dgov.models" but it's actually re-exporting from inspection. This creates an unnecessary indirection layer — consumers should import directly from `inspection.py`.
- Only one import site uses this path.

### review_fix.py
- No significant issues. Review fix pipeline is actively used.

### art.py
- [LINE:1-17] MINIMAL: Contains only `print_banner()` function. Used in one place (CLI entry point). Could be inlined or moved to a CLI utility module.

### responder.py
- [LINE:1-147] ORPHANED: `auto_respond` is imported in exactly one place (agents.py). The module handles response matching for agent communications. Thin but active.

### openrouter.py
- No significant issues. Core API integration layer for model completions.

### metrics.py
- [LINE:1-80] MINIMAL: Contains only `compute_stats` function. Used in one place. Could potentially live in inspection.py or a utils module.

### blame.py
- No significant issues. Git blame analysis functionality is used by inspection.py.

### retry.py
- [LINE:1-203] OVERLAP: Retry policy logic overlaps with `recovery.py`. The split between retry.py (policies/context) and recovery.py (operations) is somewhat unclear and could benefit from better documentation or consolidation.

## Modules to consider deleting entirely

1. **`models.py`** — Contains only a re-export. Can be deleted once all consumers import from `inspection.py` directly.
2. **`strategy.py`** — Two small helper functions that could be moved to a utilities module or the files that use them.

## Consolidation opportunities

1. **`retry.py` + `recovery.py`** — These share retry-related responsibilities. Consider merging retry policy logic into recovery.py or clearly documenting the boundary (retry.py = policy/decisions, recovery.py = execution).

2. **`dashboard.py` vs `dashboard_v2.py`** — Determine which is canonical. If v2 is newer, migrate and delete v1. If v1 is canonical, delete v2.

3. **`art.py` + `metrics.py`** — Both are minimal (1 function each). Consider moving `print_banner` to CLI and `compute_stats` to inspection.py or a utils module.

## Recommended deletions

- `models.py` — Replace imports of `MergeResult` with direct imports from `inspection.py`
- `dashboard_v2.py` — If `dashboard.py` is the active version (846 lines vs 80 lines)
- `strategy.py` — If `build_prompt_dict` and `load_prompt_from_env` can be relocated
