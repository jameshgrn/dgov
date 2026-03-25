# HANDOVER — 2026-03-25 (Plan Generation + Config System + Hardening)

## Current State

Zero open bugs. 1698 unit tests passing. 24 commits on main this session. Clean dashboard (0 panes).

## Completed (24 commits)

### Terrain tuning (5 commits, plan-driven)

1. **River threshold fix** (`a6b1bbc`) — render order >= 2 only, headwater cells no longer blue
2. **Maturity hysteresis** (`94a6c93`) — HUD label doesn't flicker near thresholds (±0.03 margin)
3. **Hour-5 keyframe** (`0c40b87`) — smoother afternoon→evening transition
4. **Performance benchmarks** (`1ecf354`) — step < 100ms, render < 200ms, full frame < 500ms
5. **Terrain tests** — 65 terrain tests total

### Plan DX (4 commits, plan-driven + governor fix)

6. **Plan scaffold** (`bdcad67`) — `dgov plan scaffold --goal "..." --files a.py` generates TOML template
7. **Cross-plan claim checks** (`628493b`) — warns on overlapping file claims between active DAGs
8. **Plan resume** (`a79a8f5`) — `dgov plan resume <file> --wait` skips merged units, re-dispatches failed
9. **Eval quality** (`d058afc`) — prompt tuning with GOOD/BAD evidence patterns, forbids AST/regex fragility

### Plan generation provider (6 commits, architecture)

10. **GENERATE_PLAN decision kind** (`30905fc`) — GeneratePlanRequest/Decision types in decision.py
11. **PlanGenerationProvider** (`36b024c`) — builds prompt, calls LLM, validates output TOML
12. **Claude CLI transport** (`3896ff0`, `c93644f`, `26094f1`) — iterating on transport: OpenRouter → claude -p → config-driven
13. **Audit sink fix** (`5674429`) — persistence._decision_kind_name didn't know GeneratePlanRequest
14. **`--auto` wired** — `dgov plan scaffold --auto --goal "..." --files a.py` calls Sonnet, returns complete plan

### Config system (3 commits, plan-driven)

15. **Config loader** (`11598a4`) — `src/dgov/config.py` with `load_config()`, deep merge, typed defaults
16. **Config CLI** (`1e06479`) — `dgov config show` prints effective config
17. **Provider wiring** (`647da22`) — PlanGenerationProvider reads transport/auth/model from config

### Hardening (4 commits, investigation + fixes)

18. **Lint scope fix** (`2d9fc7c`) — count distinct files not error lines, downgrade unfixable to warning
19. **Conflict markers** (`e0ac99b`) — `_check_conflict_markers()` rejects unresolved markers before commit
20. **Preflight superseded fix** (`ea51a3c`) — `check_file_locks()` skips superseded panes (was blocking dispatch)
21. **Test failure gate restored** (`3fe46c5`) — squash merge path had test failure check accidentally deleted by worker

## Key Decisions

- **API policy (ledger #56):** OpenRouter for open-source models (Qwen) only. Proprietary models (Anthropic, OpenAI, Google) always go through their own CLI with OAuth — never through OpenRouter. `claude -p` for Sonnet, `codex exec` for Codex, `gemini` for Gemini.
- **PlanGenerationProvider (ledger #54):** Structured co-consultation, not shared context. Governor calls provider with goal+files, gets back valid TOML. Provider is pure (no I/O) — CLI pre-reads files and examples.
- **Config system:** Single `~/.dgov/config.toml` for user preferences. Project `.dgov/config.toml` overrides. `agents.toml` stays for agent definitions only.
- **Auth:** `ANTHROPIC_API_KEY` env var in `.zshrc` was hijacking `claude -p` away from OAuth. Should be removed — OAuth subscription is the intended auth path.

## Open Issues

Zero open bugs. One env var to clean up:

| Item | Action |
|------|--------|
| `.zshrc:14` | Remove/comment `ANTHROPIC_API_KEY` export — it overrides OAuth for `claude -p` |

## Bugs Found & Fixed

| Ledger | Bug | Root Cause |
|--------|-----|------------|
| #53 | Post-merge lint "10 files" false positive | Counted error lines not files; unfixable was hard fail |
| #57 | Plan-resume dispatch fails silently | Superseded panes not in preflight skip list |
| #60 | Squash merge ignores test failures | Lint-scope worker deleted adjacent test failure check |

## Pattern Discovered (ledger #64)

Workers can accidentally delete adjacent code when editing a function. The lint-scope worker removed the test failure → validation_failed logic while downgrading lint to a warning. **Always verify surrounding logic after worker merges, especially validation gates.**

## Next Steps

1. **`dgov config set`** — CLI for quick config edits without opening TOML
2. **`dgov doctor` auth validation** — warn when config says `auth = "oauth"` but `claude auth status` shows API key
3. **`--auto --run`** — generate plan + validate + dispatch in one command
4. **Review failure reason in `--wait`** — surface why, not just "review fail"
5. **Per-plan `--wait` output** — don't interleave events from multiple DAGs
6. **Cost tracking** — accumulate OpenRouter spend per plan/session
7. **Implicit worker epilogue** — lint + format + commit as default lifecycle, not prompt boilerplate

## Important Files Changed

- `src/dgov/config.py` — NEW: unified config loader
- `src/dgov/decision.py` — GeneratePlanRequest/Decision, GENERATE_PLAN kind
- `src/dgov/decision_providers.py` — PlanGenerationProvider with config-driven transport
- `src/dgov/provider_registry.py` — GENERATE_PLAN registration
- `src/dgov/persistence.py` — GeneratePlanRequest in audit sink
- `src/dgov/cli/plan_cmd.py` — scaffold --auto, plan resume --wait
- `src/dgov/cli/admin.py` — dgov config show
- `src/dgov/merger.py` — conflict markers, lint scope fix, test failure gate restoration
- `src/dgov/preflight.py` — superseded pane skip
- `src/dgov/terrain.py` — river threshold, keyframe, Strahler order fix
- `src/dgov/terrain_pane.py` — maturity hysteresis
- `tests/test_config.py` — NEW: config loader tests
- `tests/test_terrain_perf.py` — NEW: performance benchmarks
