# HANDOVER

## Current State
- Branch: `main` at `e00ec03` (clean working tree)
- Tests: targeted unit slices passed; full suite not rerun per project policy
- Panes: none
- Status: `uv run dgov status -r .` reports `0 panes`, `18 healthy / 4 unhealthy` agents, `1` open bug

## Completed This Session
- **Refactor review info model** (`fb254c8`): split `ReviewInfo` into nested review submodels while preserving the flat external contract via compatibility properties and `to_dict()`.
- **Reject safe zero-commit reviews** (`29bf997`): zero-commit worker panes now review as `review` with `no commits — nothing to merge`; fixed ledger bug `#148`.
- **Make cleanup failure state explicit** (`9f122e3`): `_full_cleanup()` now returns a real boolean for `worktree_removal_failed`; resolved debt `#134`.
- **Refactor merge precondition checks** (`643f912`): `_check_merge_preconditions()` in `src/dgov/merger.py` now delegates to distinct helpers instead of acting as a branchy policy sink; resolved debt `#132`.
- **Drop codebase payload from worker prompts** (`815b346`): worker instruction files no longer embed full `CODEBASE.md`; fixed prompt-discipline debt `#153`.
- **Tighten governor policy rules** (`ebc9086`): added explicit rules for no dual-ownership shims, root-cause traceability, and domain-first placement.
- **Remove napkin terminology** (`de1e147`): cleaned remaining `napkin`/`.napkin` surfaces from code and docs; resolved debt `#144`.
- **Add slow-is-smooth policy** (`e00ec03`): added the rule to `CLAUDE.md` and logged it as ledger rule `#157`.

## Ledger Snapshot
### Open Bug
- #149 — pane launch/output command delivery can duplicate or garble shell snippets (`ssource ...`), causing workers to fail before real work starts

### Open Debt
- #145 — add a lightweight live worker view in tmux/TUI showing formatted messages and tool calls

### New Rules This Session
- #154 — no dual-ownership shims
- #155 — fix the first wrong layer when reachable
- #156 — domain-first placement
- #157 — slow is smooth, smooth is fast

## Key Verification
- `uv run ruff check src/dgov/inspection.py tests/test_inspection.py`
- `uv run pytest tests/test_inspection.py -q -m unit`
- `uv run pytest tests/test_executor.py -q -m unit -k 'review or merge_gate'`
- `uv run ruff check src/dgov/lifecycle.py tests/test_lifecycle.py`
- `uv run pytest tests/test_lifecycle.py -q -m unit -k 'full_cleanup or worktree_removal_failed'`
- `uv run ruff check src/dgov/merger.py src/dgov/lifecycle.py tests/test_lifecycle.py tests/test_merger_coverage.py tests/test_concurrent_merge.py`
- `uv run pytest tests/test_lifecycle.py -q -m unit -k 'worker_prompts_omit_codebase_payload or git_excludes_dgov_worker_instructions'`
- `uv run pytest tests/test_merger_coverage.py tests/test_concurrent_merge.py -q -m unit -k 'strict_claims or dirty_worktree or illegal_state or attached_agent'`
- `uv run ruff check src/dgov/cli/briefing_cmd.py src/dgov/cli/ledger_cmd.py src/dgov/spans.py`

## Lookup Cache
- `src/dgov/inspection.py` — zero-commit review artifacts no longer surface as `safe`; `review_worker_pane()` emits `review_fail` with `no commits — nothing to merge`.
- `src/dgov/lifecycle.py` — worker prompts are now slim on both system-prompt and fallback instruction paths; no more embedded `CODEBASE.md` payload.
- `src/dgov/merger.py` — merge preconditions are now split into `_state_precondition_result()`, `_warn_squash_overlap()`, and `_claim_violation_result()`.
- `CLAUDE.md` — policy core now explicitly includes no dual-ownership shims, root-cause traceability, domain-first placement, and slow-is-smooth.
- `src/dgov/cli/briefing_cmd.py` — `.napkin` is no longer treated as a special hidden report name.

## Open Issues
- Bug `#149` is still the main operational correctness problem. The pane bootstrap path can still garble pasted shell commands before workers do any real work.
- Agent health is still degraded (`18 healthy / 4 unhealthy`). Investigate before leaning on retries/escalation or local tunnel-backed workers.
- Claude-side process docs are still stale in places:
  - `.claude/skills/dgov/SKILL.md`
  - `.claude/commands/dgov-dispatch.md`
  - `.claude/commands/dgov-handover.md`

## Next Steps
- If continuing the operator-experience track, tackle debt `#145` as a tightly scoped read-only live worker view that consumes existing state/logs/spans only.
- If continuing reliability work first, fix bug `#149` in the tmux command-delivery/bootstrap path.
- Update the stale Claude skill/command docs so they match current role-based routing and plan-first governor policy.
