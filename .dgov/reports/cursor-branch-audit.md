# Cursor Branch Safety Audit

**Date:** 2026-03-17
**Auditor:** Hunter (worker)
**Scope:** Verify cursor/cursor-auto agents commit to their worktree branch, not main

## Hook Verification Results

### 1. SHARED Section ("Your environment")
✅ **PRESENT** — The worktree_created hook writes a `## Your environment` section containing:
- `Working directory: ${_WORKTREE_PATH}` — absolute path to the worktree
- `Branch: ${_SLUG}` — the worker's branch name
- `Main repo: ${_ROOT}` — explicitly labeled "(do NOT cd here or commit here)"

Also includes a verification reminder: "Before committing, ALWAYS verify: `pwd` shows your worktree path and `git branch` shows * ${_SLUG}"

### 2. Cursor-specific Workflow (cursor|cursor-auto)
✅ **PRESENT** — The cursor case block includes:
- **Step 1:** "Run `pwd` and `git branch` to confirm you are in the worktree on the correct branch."
- **Step 7:** "Verify location: run `pwd` and `git branch` — if either is wrong, STOP."
- Steps 8 (git commit) and 9 (dgov worker complete) are marked CRITICAL with explicit warning text

### 3. .cursorrules Symlink
✅ **PRESENT** — Bottom of hook has:
```bash
case "${_AGENT}" in
  cursor|cursor-auto)  _NATIVE_FILE=".cursorrules" ;;
esac
```
This creates `ln -sf CLAUDE.md "${_WORKTREE_PATH}/${_NATIVE_FILE}"`, ensuring cursor reads the same worker instructions.

### 4. Headless Cursor Trust Block (lifecycle.py ~line 348)
✅ **PRESENT** — In `_setup_and_launch_agent()`:
- **Interactive mode:** After `send_keys_ready_delay_ms`, sends `"a"` key, then sleeps 2s before sending prompt
- **Headless mode (line ~378):** After launching, sleeps 3 seconds, then sends `"a"` key via `backend.send_keys(pane_id, ["a"])`
- Both paths gated by `is_cursor = agent_id in ("cursor", "cursor-auto") or agent_def.prompt_command == "cursor-agent"`

## Protections That Exist

1. **Environment variables:** `DGOV_WORKTREE_PATH`, `DGOV_BRANCH`, `DGOV_ROOT` are injected into the pane before the agent starts
2. **Working directory:** Pane is created with `cwd=worktree_path`, so `pwd` will always show the worktree
3. **Hook-generated CLAUDE.md:** Contains explicit branch/worktree verification instructions and warnings against cd'ing to main
4. **Cursor-native instructions:** .cursorrules symlink ensures cursor reads the same guardrails
5. **Prompt path rewriting:** `re.sub()` rewrites absolute paths from project_root to worktree_path so agents edit the right files
6. **Pre-merge-commit hook:** Blocks workers from merging/pulling/rebasing (installed by `_install_worker_hooks`)
7. **Workspace trust acceptance:** Headless cursor gets "a" key to accept workspace trust dialog automatically

## Gaps / Risks

1. **Symlink is to CLAUDE.md, not a cursor-specific file:** Cursor reads .cursorrules but gets the same content as CLAUDE.md. The cursor-specific section is appended to the same file, which is fine — but if cursor's parser is picky about markdown structure, the shared sections above could cause issues.

2. **No enforcement of `pwd` check:** The hook tells cursor to run `pwd` and `git branch` in steps 1 and 7, but nothing prevents cursor from skipping these steps. It's guidance, not enforcement.

3. **Trust dialog timing:** The 3-second sleep before sending "a" is a heuristic. On slow systems, the trust dialog may not be ready yet. On fast systems, it wastes time. No retry logic exists.

4. **`assume-unchanged` on .cursorrules:** The hook runs `git update-index --assume-unchanged` on .cursorrules, which prevents it from showing in `git status`. This is correct behavior but could confuse agents that expect all files to be tracked.

5. **No git hook to block commits to main:** The pre-merge-commit hook blocks merge/pull/rebase, but there's no pre-commit hook that checks "am I on the correct branch?" An agent could theoretically `git checkout main` and commit there. (This is mitigated by the worktree setup — main isn't checked out in the worktree.)

## Verdict

**Hook changes are SUFFICIENT for normal operation.** The combination of environment variables, working directory, hook-generated instructions, cursor-specific workflow steps, and .cursorrules symlink provides strong guidance. The workspace trust acceptance in headless mode addresses a real operational blocker.

The gaps are edge cases (timing, agent non-compliance with guidance) rather than structural flaws. The system relies on instruction-following rather than hard enforcement, which is appropriate for LLM agents — you can't truly "force" an LLM, but you can make the correct path obvious and the wrong path require deliberate deviation.

**Recommendation:** No additional hook changes needed. Consider adding a post-commit hook that warns if a commit lands on `main` in a worktree context, as a last-line defense.
