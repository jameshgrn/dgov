# Handover: closed batch isolation and DAG cleanup carry-over

## Session context
- Date: 2026-03-29T21:58:11Z
- Branch: main @ 3a0a5e5
- Last commit: Stabilize DAG routing and persistence

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| none | closed | No active or preserved panes remain. |

## Open bugs/issues
- None.

## Blockers/debt
- `uv run dgov tunnel` reported the River tunnel as up, but `http://localhost:8080/health` still returned unreachable immediately afterward.
- Branch is ahead of `origin/main` and still needs the pre-push gate plus push if remote sync is desired.

## Next steps
1. Run the pre-push verification gate on `main`.
2. Push `main` to `origin/main` if the gate passes.
3. If local-worker dispatch is needed, re-check tunnel health and inspect why the local `8080` health endpoint is still unreachable after refresh.

## Notes
- Commit `3a0a5e5` merged the governor-exception fixes for pane-batch isolation, DAG root propagation, and monitor snapshot persistence.
- Ledger bug `#240` was resolved as fixed.
- Historical DAG run `164` was cancelled, the pane `r164-fix-dag-cli-root` was closed, and the stale local branch was deleted.
- `uv run dgov status -r . --json` reported zero panes and zero preserved panes.
