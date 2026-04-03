# Conflict resolution

Merging changes from multiple workers can occasionally lead to conflicts. dgov uses a sophisticated "plumbing" merge strategy to minimize risks and offers both manual and AI-assisted resolution paths.

## Plumbing merge

By default, dgov uses `git merge-tree` (a low-level "plumbing" command) to compute merges in memory before touching your repository's working tree.

1. **Safety**: If a merge has conflicts, dgov detects it before modifying any files in your main worktree.
2. **In-memory**: The actual merge commit is created via `commit-tree` without needing to `checkout` the files.
3. **Atomic**: The main branch ref is only updated if the merge computation is clean.

## Resolution strategies

The kernel attempts to auto-resolve conflicts during the merge phase. If this fails, the task enters `merge_conflict` state.

| Strategy | Description |
|----------|-------------|
| **Agent** | **(Default)** The kernel spawns a resolver agent to fix markers automatically. |
| **Manual**| Leaves conflict markers for manual resolution. |

## AI-assisted resolution (Agent)

When conflicts are detected and the `agent` strategy is selected:

1. **Spawn**: The kernel spawns a new resolver pane (it prefers `claude`, falling back to `codex`) in the current worktree.
2. **Markers**: It executes `git merge --no-commit` to put the standard git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) into the files.
3. **Wait**: It waits for the resolver agent to finish fixing all markers and `git add` the resolved files.
4. **Finalize**: If all markers are gone, the kernel completes the merge commit. Otherwise, it aborts the merge to a clean state.

## Manual resolution

If you prefer to resolve conflicts yourself:

1. **Inspect**: Use `dgov pane review <slug>` and `dgov pane diff <slug>` to see the changes.
2. **Enter worktree**: Navigate to the worker worktree (shown in `dgov pane list`).
3. **Resolve**: Fix the conflicts manually in your editor.
4. **Signal**: Run `dgov pane signal <slug> --done` to mark the pane as resolved.
5. **Cleanup**: The kernel will complete the merge and clean up automatically.

## Post-merge linting

After every successful merge (regardless of whether there were conflicts), dgov runs:
- `ruff check --fix`
- `ruff format`

It then **amends** the merge commit to include these fixes. This ensures that the `main` branch always stays lint-clean.

## Protected files

If a worker modifies a **protected file** (like `CLAUDE.md`), dgov ignores those changes. It automatically checks out the `main` branch version of those files and amends the worker's branch **before** attempting the merge.

## Freshness and stale merges

dgov calculates a **freshness** score for each pane. If your `main` branch has moved forward significantly, the pane is marked as `warn` or `stale`.

In these cases, you should **rebase** the worker branch before the kernel attempts the merge to avoid unnecessary conflicts:

```bash
# In the worker's worktree:
git rebase main
```
