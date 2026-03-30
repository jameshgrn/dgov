# Handover: control-plane hardening landed and doctor is green

## Session context
- Date: 2026-03-29T20:57:45-0400
- Branch: main @ f936b4c
- Last commit: Prefer project routing in health summary

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| none | closed | No active or preserved panes remain. |

## Open bugs/issues
- None.

## Blockers/debt
- No tracked blockers.
- Operational note: `recent failures: 6` in `dgov status` is historical event noise, not an open ledger bug.

## Next steps
1. Push `main` to `origin/main` when ready.
2. If you want another hardening pass, the remaining surfaced risk is real capacity, not stale state: the 4 routed unhealthy backends are the down MLX pool, 3 routed backends are unprobed frontier tools, and 5 optional unavailable backends are outside the repo's local routing policy.

## Notes
- Landed stale-worktree cleanup on main:
  `0037e3b` Harden stale worktree cleanup
  `a6cdb49` Merge r175-harden-stale-worktree-cleanup
- Landed routing/health reporting hardening on main:
  `7775b18` Harden agent health reporting
  `f936b4c` Prefer project routing in health summary
- `uv run dgov doctor -r .` now passes fully, including `no orphaned worktrees -- 0 tracked`.
- Final status at handoff:
  `dgov status: 0 panes`
  `agents: 15 routed, 8 healthy, 4 routed unhealthy, 3 unprobed, 5 optional unavailable`
  `recent failures: 6`
- Verification run on main for the final hardening passes:
  `uv run ruff check` / `uv run ruff format --check`
  `uv run ty check`
  targeted unit slices for stale worktree cleanup, router/admin health reporting, and status summary fix-forward all passed.
