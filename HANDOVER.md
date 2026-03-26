# HANDOVER — 2026-03-26 (Discipline Audit + MLX Fleet + Bug Fixes)

## Current State

Zero active panes. MLX 9B pool operational (4 instances). All discipline refactors landed. Three bugs fixed. Clean main.

## Completed

### Discipline Audit (clanker-discipline skill)
- Audited dgov against 4 rules: derive-don't-store, impossible states, function contracts, data-over-procedure
- 20 files changed, +276/-155 lines across 17 commits
- **Rule 1 (derive)**: `PreflightReport.passed`, `PaneTrajectory.outcome/total_duration_ms`, `SemanticManifest.claim_violations` → `@property`
- **Rule 2 (sentinels)**: 25+ `str = ""` fields → `str | None = None` across 8 source files + 4 test files
- **Rule 3 (contracts)**: `review_worker_pane` split into pure `_inspect_worker_pane` + event wrapper; `_run_lint_checks` → `_apply_lint_fixes`
- **Rule 4 (tables)**: 4 if/elif chains → dicts (terrain glyphs, monitor event factory, preflight fixer registry, executor cleanup policy)

### Bug Fixes
- **Triple retry** (ledger #94, fixed): `_pane_work_already_on_main()` guard in monitor + `maybe_auto_retry()`. Checks `git merge-base --is-ancestor` before retrying.
- **Codex double-exec** (fixed): `build_launch_command` produced `codex exec exec -m ...`. Now strips leading `exec` from default_flags when force_headless adds it.
- **CLI test failures** (fixed): Updated 3 test assertions for command restructure (pane preflight, agent list, close error format, parent_slug None).

### MLX 9B Worker Pool
- 4x `mlx_lm.server` 0.31.1 on ports 8088-8091
- Model: `mlx-community/Qwen3.5-9B-MLX-4bit`, thinking disabled
- Sampling: `--temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0`
- Routing: River first → MLX overflow → OpenRouter fallback
- ~85 tok/s on M3 Max. Smoke tested end-to-end.
- Script: `~/bin/mlx-9b` (start/stop/status)

## Key Decisions

- **River-first routing**: faster GPU, MLX provides 4 parallel overflow slots
- **mlx_lm.server over mlx_vlm.server**: 0.31.1 supports Qwen 3.5 natively, has `--chat-template-args`
- **Speculative decoding not viable yet**: 0.8B draft corrupts output (VL/text architecture mismatch)
- **LT-GOV sub-dispatch plumbing exists**: `DGOV_PROJECT_ROOT` env var, `_autocorrect_roots`, LT-GOV template. Needs design decision on worktree vs no-worktree.

## Open Issues

- **LT-GOV architecture** (next step): Should LT-GOVs get worktrees? They don't edit code — just read and dispatch. No-worktree or branch-only mode would be simpler. Plumbing for dispatch from worktrees already works.
- **Ledger #84**: Plumbing merge clobbers governor changes to files workers also touch
- **Ledger #83**: Prompt-inferred file claims too aggressive
- **Ledger #82**: File claim conflicts not enforced properly
- **Ledger #74**: Concurrent plan run --wait sees cross-run events

## Next Steps

1. **Design LT-GOV sub-governor mode** — worktree vs no-worktree vs branch-only. Key insight: LT-GOVs read broadly + dispatch workers. They don't need isolated file state. Could just run in a tmux pane with `DGOV_PROJECT_ROOT` set.
2. **Remaining discipline items** (optional): `NewType` domain concepts, `StrEnum` for pane states, discriminated unions
3. **Speculative decoding** (optional): needs matching text-only 9B + 0.8B pair

## Important Files

- `src/dgov/monitor.py` — retry guard (`_pane_work_already_on_main`)
- `src/dgov/agents.py` — codex double-exec fix, `build_launch_command`
- `src/dgov/inspection.py` — contract split (`_inspect_worker_pane` + `_apply_lint_fixes`)
- `src/dgov/templates.py:88-101` — LT-GOV dispatch template
- `src/dgov/cli/pane.py:16-24` — `_autocorrect_roots` (worktree → main redirect)
- `src/dgov/lifecycle.py:863` — `DGOV_PROJECT_ROOT` env var injection
- `~/bin/mlx-9b` — MLX pool launcher
- `~/.dgov/agents.toml` — MLX agent config + routing chains
- `~/.pi/agent/models.json` — pi provider URLs for MLX (ports 8088-8091/v1)
