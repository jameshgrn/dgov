# Handover: Flat File Claims Feature

**Date:** 2026-04-08
**Branch:** `main` @ `11cd8042` (pushed)
**Context:** Implemented Option B from file claim UX analysis — flat `files = [...]` shorthand replacing verbose `files.edit/create/delete` for common cases.

---

## Current State

- `main` is clean and pushed. 532+ tests passing.
- Flat file claims feature shipped: plan authors can use `files = ["a.py", "b.py"]` instead of separate `files.edit`/`files.create`/`files.delete` blocks.
- Old structured format still works — full backward compatibility.

---

## Completed

| Change | File(s) |
|--------|---------|
| Added `touch` field to `DagFileSpec` (Pydantic model) | `dag_parser.py` |
| Added `touch` field to `PlanUnitFiles` (frozen dataclass) | `plan.py` |
| Parse `files = [list]` as touch shorthand in DAG parser | `dag_parser.py` |
| Handle list vs dict for `files` in plan tree merger | `plan_tree.py` |
| Include `touch` in runner's `task_files` + `file_claims` flattening | `runner.py` |
| Smart serialization: pure touch → `files = [...]`, mixed → subtable | `serializer.py` |
| Updated CLI display + example TOML template | `cli/plan.py` |
| Updated plan authoring guide | `CLAUDE.md` |
| 15 new tests across dag_parser, plan, plan_tree, serializer, runner | `tests/` |

---

## Key Decisions

- **`touch` field over collapsing into `edit`**: Keeps the semantic distinction clean. `touch` = "I'll modify these files, auto-classify create vs edit at dispatch time." Doesn't conflate with explicit `edit`.
- **Backward compatible, not replacing**: Old `files.edit/create/delete` still works. Both formats coexist in the same plan. No migration needed.
- **Conflict detection covers `touch`**: `_all_touches()` and `validate_plan()` detect overlaps between `touch` and `edit`/`create`/`delete` across independent tasks.
- **Serializer is context-aware**: Pure touch emits `files = [...]` (round-trips as list). Mixed touch+delete emits `files.touch = [...]` + `files.delete = [...]` (subtable format).
- **Rejected auto-inference approach**: AST-walking prompt tokens for symbol→file matching was fragile, broke conflict detection for vague prompts, and disabled scope enforcement as fallback. Flat list is simpler and preserves all invariants.

---

## Open Issues

- **Runner contention under 6+ parallel tasks** — `ThreadPoolExecutor` interaction undiagnosed.
- **No token/cost tracking** — no visibility into API spend per run.
- **No semantic review** — `review_sandbox()` is git sanity checks only.
- **Sentrux scans scratch/test `.py` files** — any `.py` with functions in a git-tracked dir increases complexity count.

---

## Next Steps

### 1. Dogfood flat files format
- Author a new plan using `files = [...]` shorthand exclusively.
- Verify compile → validate → run → merge pipeline end-to-end.
- Check `dgov watch` output still renders correctly.

### 2. Token/cost tracking (from prior handover)
- Workers emit token counts via `on_event` callback.
- Runner aggregates in `_task_durations`-style dict.
- `_append_run_log` + CLI exit summary include cost.

### 3. Runner contention profiling
- Add timing instrumentation around `ThreadPoolExecutor` calls in `_merge`.
- Run a plan with 8+ parallel tasks and check for >10s stalls.

---

## Important Files

- `/Users/jakegearon/projects/dgov/src/dgov/dag_parser.py` — `DagFileSpec.touch`, flat list parse in `parse_dag_file`
- `/Users/jakegearon/projects/dgov/src/dgov/plan.py` — `PlanUnitFiles.touch`, `_all_touches()` includes touch
- `/Users/jakegearon/projects/dgov/src/dgov/plan_tree.py` — `_unit_from_task()` handles list vs dict vs error
- `/Users/jakegearon/projects/dgov/src/dgov/runner.py` — `task_files` and `file_claims` include touch
- `/Users/jakegearon/projects/dgov/src/dgov/serializer.py` — smart flat vs subtable emission
- `/Users/jakegearon/projects/dgov/src/dgov/cli/plan.py` — `_format_unit_files()`, example TOML template
