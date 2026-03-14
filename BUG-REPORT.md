# dgov Bug Report — Naive User Testing

Tested from a worktree (`bug-hunt`) with `DGOV_SKIP_GOVERNOR_CHECK=1`.
dgov v0.5.0, Python 3.14.3, macOS Darwin 25.3.0.

---

## Crashes (unhandled tracebacks)

### 1. Invalid slug raises unhandled `ValueError` (exit 1, traceback)

**Commands:**
```
dgov pane create -a claude -p 'test' -r . -s UPPERCASE
dgov pane create -a claude -p 'test' -r . -s 'has spaces'
dgov pane create -a claude -p 'test' -r . -s this-is-a-really-long-slug-name-that-exceeds-fifty-characters-limit
```

**Result:** Full Python traceback ending in:
```
ValueError: Invalid slug: 'UPPERCASE'. Must be 1-50 chars, lowercase alphanumeric and hyphens, starting with alphanumeric.
```

**Expected:** A clean error message to stderr and exit 1, not a traceback. The `ValueError` from `strategy.py:191` is not caught by `cli.py:pane_create`. Should wrap slug validation in a try/except in the CLI layer.

### 2. Template `bugfix` crashes with `KeyError: 'test_file'` on render

**Command:**
```
dgov pane create -T bugfix --var file=src/foo.py --var 'description=off-by-one' --no-preflight -r .
```

**Result:** Full Python traceback ending in:
```
KeyError: 'test_file'
```

**Root cause:** The bugfix template uses `{test_file}` in its template string:
```
"Read {file}. Find the bug described: {description}. Fix it. Run tests with: uv run pytest {test_file} -q. ..."
```
But `required_vars` only lists `["file", "description"]` — `test_file` is not listed as required. When the user follows `template show bugfix` (which tells them only `file` and `description` are needed), rendering crashes. Either `test_file` should be in `required_vars`, or the template should use a default/optional mechanism.

### 3. `review-fix -t nonexistent-file.py` hangs indefinitely

**Command:**
```
dgov review-fix -t nonexistent-file.py
```

**Result:** Hangs — appears to dispatch an actual review agent for a file that doesn't exist. No validation that the target file exists before dispatching. Had to kill the process. Also created a real pane (`review-000-nonexistent-file`) that was later merged by an unrelated `merge-all` call.

---

## Missing validation

### 4. Empty prompt accepted — creates real pane

**Command:**
```
dgov pane create -a claude -p '' -r .
```

**Result:** Exit 0. Created a real worktree, tmux pane, and launched claude with an empty prompt. The auto-generated slug was `generate-short-kebab-slug` — the slug generator received an empty string and output its own instruction text as the slug.

**Expected:** Reject empty prompts with a clean error: "Prompt cannot be empty."

### 5. Checkpoint create silently overwrites duplicates

**Commands:**
```
dgov checkpoint create test-checkpoint  # exit 0
dgov checkpoint create test-checkpoint  # exit 0, no warning
```

**Result:** Second call silently overwrites the first checkpoint. No warning, no confirmation, no `--force` flag required.

**Expected:** Either error on duplicate name, or warn that the previous checkpoint is being overwritten.

### 6. Preflight `git_branch` passes when it can't determine the branch

**Command:**
```
dgov preflight -a claude -r /nonexistent/path
```

**Result:** The `git_branch` check reports `passed: true` with message "Could not determine branch: [Errno 2] No such file or directory". Similarly, `stale_worktrees` reports `passed: true` with "Could not list worktrees: [Errno 2]".

**Expected:** A check that fails with an error should not report `passed: true`. If the check can't run, it should either fail or be marked as skipped.

### 7. Preflight `git_branch` passes on non-main branch

**Command:**
```
dgov preflight -a claude -r .
```

**Result:** `git_branch` reports `passed: true` with message "On branch 'bug-hunt'". The governor is supposed to be on main, but the preflight doesn't flag this.

**Expected:** Since dgov enforces main-branch operation, `git_branch` should warn (or at least note) when not on main. The `DGOV_SKIP_GOVERNOR_CHECK` env var bypasses the CLI guard but preflight should still report the actual branch state accurately.

### 8. Preflight `stale_worktrees` lists main repo as stale

**Command:**
```
dgov preflight -a claude -r .
```

**Result:**
```
"stale_worktrees": passed=false, "2 stale worktree(s): /Users/jakegearon/projects/dgov, ..."
```

The main repository path itself (`/Users/jakegearon/projects/dgov`) is listed as a stale worktree. This is a false positive — the main repo is not a stale worktree.

---

## Inconsistent exit codes

### 9. `pane logs` exits 0 on error

**Command:**
```
dgov pane logs nonexistent-slug
```

**Result:** Outputs `{"error": "No log file found: ..."}` but exits 0.

**Expected:** Exit 1 when there's an error, consistent with all other pane commands (`close`, `review`, `capture`, etc.) which exit 1 on "not found".

### 10. `pane resume` exits 0 on error

**Command:**
```
dgov pane resume nonexistent-slug
```

**Result:** Outputs `{"error": "Pane not found: nonexistent-slug"}` but exits 0.

**Expected:** Exit 1, consistent with `close`, `review`, `escalate`, etc.

---

## Bad error messages

### 11. `pane wait` on nonexistent slug runs full timeout instead of failing fast

**Command:**
```
dgov pane wait nonexistent-slug --timeout 1
```

**Result:** Waits the full 1 second, then reports:
```
{"error": "Timeout after 1s", "slug": "nonexistent-slug", "agent": "unknown"}
```

**Expected:** Immediately report "Pane not found: nonexistent-slug" instead of waiting for the timeout. With longer timeouts this wastes significant time. The "agent: unknown" is confusing — it's not that the agent is unknown, it's that the pane doesn't exist.

---

## Inconsistencies

### 12. `--session-root` uses `-S` everywhere except `resume` and `logs` (which use `-R`)

**`pane wait --help`:** `-S, --session-root TEXT`
**`pane resume --help`:** `-R, --session-root TEXT`
**`pane logs --help`:** `-R, --session-root TEXT`

A user who learns `-S` from one command will be confused when it doesn't work on `resume` or `logs`.

### 13. `-r` help text is inconsistent across commands

| Command | `-r` help text |
|---------|---------------|
| `agents` | "Project root for registry loading" |
| `status` | "Git repo root" |
| `pane create` | "Git repo root for the worktree" |
| `preflight` | "Git repo root" |
| `blame` | "Project root" |
| `resume` | "Project root" |

These all mean the same thing but are described differently.

### 14. Help descriptions truncated in `dgov pane --help`

```
escalate   Escalate a worker pane to a different agent (e.g.
util       Launch a utility pane (e.g.
resume     Resume a pane by re-launching an agent in its existing...
```

The descriptions are cut off mid-sentence. Click truncates based on terminal width, but the descriptions should be written to be meaningful even when truncated (put the key info first).

### 15. Worktrees nest inside worktrees when run from a worktree

**Command:**
```
dgov pane create -a claude -p 'test task' --no-preflight -r .
```

**Result (worktree path):**
```
/Users/jakegearon/projects/dgov/.dgov/worktrees/bug-hunt/.dgov/worktrees/test-task-description
```

This is a worktree inside a worktree. When `-r .` is used from within a worktree, it creates nested `.dgov/worktrees/` under the worktree instead of resolving to the main repo. This would cause state fragmentation — the main repo's `.dgov/state.db` wouldn't know about these panes, and the worktree's local `.dgov/state.db` would be orphaned.

### 16. `dgov` bare command says "governor ready" even in a worktree

**Command:**
```
dgov
```

**Result:** `bug-hunt — governor ready` (exit 0)

This is misleading — we're in a worktree, not the governor. With `DGOV_SKIP_GOVERNOR_CHECK=1` the guard is bypassed, but the bare command still announces "governor ready" using the worktree name. It should either respect the worktree check for bare invocation, or at minimum say "worktree: bug-hunt" instead.

---

## UX issues

### 17. `pane list` shows header + separator even with no panes

**Command:**
```
dgov pane list
```

**Result:**
```
Slug                 Agent      State      Alive  Done  Freshness Duration     Prompt
-------------------------------------------------------------------------------------
```

A blank table with just headers. Better UX: print "No panes." when the list is empty (still showing the header is noise).

### 18. `template create` only prints to stdout — doesn't create a file

**Command:**
```
dgov template create test-template
```

**Result:** Prints TOML skeleton to stdout with a comment `# Save to .dgov/templates/test-template.toml`. The user has to manually redirect and create the directory.

**Expected:** Either create the file directly (with confirmation), or at minimum print the full command to create it:
```
mkdir -p .dgov/templates && dgov template create test-template > .dgov/templates/test-template.toml
```

### 19. `experiment log`/`summary` on nonexistent program returns empty results silently

**Commands:**
```
dgov experiment log -p nonexistent     # returns []
dgov experiment summary -p nonexistent  # returns {"total": 0, ...}
```

Both exit 0 with empty/zero results. A user who misspelled their program name would get no indication that the program doesn't exist. Should warn or suggest existing programs.

### 20. `pane classify` requires local Qwen 4B model

**Command:**
```
dgov pane classify 'fix the bug in parser.py'
```

**Result:** Returned `{"recommended_agent": "pi"}` — worked because the model was available. But there's no documentation in `--help` about the dependency on a local model. If the model isn't available, the error would be opaque.

### 21. `pane list --json` outputs valid JSON but default format table doesn't

This is by design (table vs JSON modes), but it's worth noting that scripting users must always use `--json`. The table format can't be piped to `jq` etc. The `--json` flag should probably be mentioned in the error output when piping is detected (stdout is not a tty).

---

## Things that worked well

- **`dgov version`**: Clean JSON output, correct version.
- **`dgov agents`**: Clear listing with installed status, transport type, and source.
- **`dgov status`**: Comprehensive JSON with panes, tunnel, kerberos.
- **`pane create` with nonexistent agent**: Clean rejection with available agent list.
- **`pane create` with no prompt/template**: Clean error message.
- **`pane create` with nonexistent path**: Caught by preflight with detailed check report.
- **`pane close nonexistent-slug`**: Clean JSON error, exit 1.
- **`pane review nonexistent-slug`**: Clean JSON error, exit 1.
- **`pane merge nonexistent-slug`**: Clean JSON error, exit 1.
- **`template list`/`show`/`create`**: All work correctly with good output.
- **`checkpoint create`/`list`**: Clean JSON output.
- **`blame`**: Returns structured results, handles nonexistent files gracefully (empty history).
- **`preflight`**: Comprehensive checks with clear pass/fail per check.
- **`batch --dry-run`**: Clean tier computation output.
- **`pane classify`**: Returns structured recommendation.
- **`pane prune` with nothing to prune**: Clean empty result.
- **`rebase`**: Works correctly from worktree with skip-governor-check.
- **All JSON output**: Consistently uses `json.dumps` with `indent=2`, parseable.
- **`batch nonexistent.json`**: Click's `exists=True` catches it with a clean error message.
- **`review-fix` with no targets**: Click catches the missing required option cleanly.

---

## Summary

| Category | Count |
|----------|-------|
| Crashes (tracebacks) | 3 |
| Missing validation | 5 |
| Inconsistent exit codes | 2 |
| Bad error messages | 1 |
| Inconsistencies | 5 |
| UX issues | 5 |
| Working well | 20+ |

**Highest priority fixes:**
1. Empty prompt accepted (creates real pane + tmux pane + worktree — resource leak)
2. Invalid slug raises unhandled ValueError (traceback to user)
3. `review-fix -t nonexistent-file.py` hangs forever (no target validation)
4. Bugfix template `required_vars` missing `test_file` (guaranteed crash on use)
5. `pane logs` and `pane resume` exit 0 on error (breaks scripting)
