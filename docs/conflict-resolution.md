# Conflict resolution

Merging changes from multiple workers can occasionally lead to conflicts. dgov uses a sophisticated "plumbing" merge strategy to minimize risks and offers both manual and AI-assisted resolution paths.

## Plumbing merge

By default, dgov uses `git merge-tree` (a low-level "plumbing" command) to compute merges in memory before touching your repository's working tree.

1. **Safety**: If a merge has conflicts, dgov detects it before modifying any files in your main worktree.
2. **In-memory**: The actual merge commit is created via `commit-tree` without needing to `checkout` the files.
3. **Atomic**: The main branch ref is only updated if the merge computation is clean.

## Resolution strategies

Use the `--resolve` flag during `dgov pane land` to specify how conflicts should be handled.

| Strategy | Flag | Description |
|----------|------|-------------|
| **Agent** | `--resolve agent` | **(Default)** Spawns a resolver agent to fix markers. |
| **Manual**| `--resolve manual` | Leaves conflict markers in your worktree for you. |

## AI-assisted resolution (Agent)

When conflicts are detected and the `agent` strategy is selected:

1. **Spawn**: dgov spawns a new resolver pane (it prefers `claude`, falling back to `codex`) in the current worktree.
2. **Markers**: It executes `git merge --no-commit` to put the standard git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) into the files.
3. **Wait**: It waits for the resolver agent to finish fixing all markers and `git add` the resolved files.
4. **Finalize**: If all markers are gone, dgov completes the merge commit. Otherwise, it aborts the merge to a clean state.

## Manual resolution

If you prefer to resolve conflicts yourself:

1. **Markers**: dgov places conflict markers in your working tree.
2. **Fix**: You resolve the conflicts manually in your editor.
3. **Finish**: You must `git commit` the merge yourself.
4. **Cleanup**: Run `dgov pane close <slug>` to remove the worker worktree.

## Post-merge linting

After every successful merge (regardless of whether there were conflicts), dgov runs:
- `ruff check --fix`
- `ruff format`

It then **amends** the merge commit to include these fixes. This ensures that the `main` branch always stays lint-clean.

## Protected files

If a worker modifies a **protected file** (like `CLAUDE.md`), dgov ignores those changes. It automatically checks out the `main` branch version of those files and amends the worker's branch **before** attempting the merge.

## Freshness and stale merges

dgov calculates a **freshness** score for each pane. If your `main` branch has moved forward significantly, the pane is marked as `warn` or `stale`.

In these cases, you should **rebase** the worker branch before merging to avoid unnecessary conflicts:

```bash
# In the worker's worktree:
git rebase main
```
