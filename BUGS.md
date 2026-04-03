# dgov Bug Reports

## [BUG-001] Redundant Approval Mode Flag Duplication
**Date:** 2026-03-17
**Reporter:** Research Governor (via User Hint)
**Severity:** High (Crashes agent initialization)

### Description
When a worker pane is created with an explicit `--approval-mode yolo` flag via the `-f` option, and the project is configured with `governor_permissions = "bypassPermissions"`, `dgov` appears to append a second `--approval-mode yolo` flag. This results in the agent receiving `--approval-mode yolo,yolo`, which is an invalid value and causes a critical error.

### Reproduction
1. Set `governor_permissions = "bypassPermissions"` in `.dgov/config.toml`.
2. Run `dgov pane create -a gemini -s test-pane -p "test" -f "--approval-mode yolo"`.
3. Agent fails with `Error: Invalid approval mode: yolo,yolo`.

### Expected Behavior
`dgov` should detect if an approval mode is already specified in the extra flags and not append its own, or it should merge them safely.

---
