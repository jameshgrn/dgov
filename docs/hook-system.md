# Hook system

dgov's hook system allows you to customize the lifecycle of worker panes. You can use hooks to set up worker environments, restore protected files, or run custom linting and validation after a merge.

## Hook search order

dgov searches for executable files with specific names in the following directories, in priority order (first found wins):

1. **`.dgov-hooks/`**: Version-controlled hooks for your team.
2. **`.dgov/hooks/`**: Local, gitignored hooks for your specific workstation.
3. **`~/.dgov/hooks/`**: Global hooks that run for every project.

## Available hooks

- **`worktree_created`**: Runs after the git worktree and tmux pane are created, but **before** the agent process is launched.
- **`pre_merge`**: Runs immediately before merging a worker's branch into `main`.
- **`post_merge`**: Runs after a successful merge.
- **`before_worktree_remove`**: Runs before the worker's worktree is deleted.

## Environment variables

Every hook receives the following context in its environment:

- `DGOV_ROOT`: absolute path to the main repository root.
- `DGOV_SLUG`: the unique identifier for the pane.
- `DGOV_PROMPT`: the task prompt.
- `DGOV_AGENT`: the ID of the agent running the task.
- `DGOV_WORKTREE_PATH`: absolute path to the worker's worktree.
- `DGOV_BRANCH`: the name of the worker branch.

## Example: worktree_created

Use this hook to write a custom `CLAUDE.md` to every worker's worktree. Save this as `.dgov-hooks/worktree_created` and make it executable (`chmod +x`).

```bash
#!/bin/bash
set -euo pipefail

# Add worker-specific instructions to CLAUDE.md
cat <<EOF > "$DGOV_WORKTREE_PATH/CLAUDE.md"
# Worker Context: $DGOV_SLUG
- You are running as agent: $DGOV_AGENT
- Task: $DGOV_PROMPT
- Please focus ONLY on the files related to this task.
EOF

# Ensure dependencies are in sync for this specific worktree
cd "$DGOV_WORKTREE_PATH"
uv sync --quiet
```

## Example: pre_merge

Use this hook to ensure specific files are restored to their `main` branch state before the merge happens.

```bash
#!/bin/bash
set -euo pipefail

# Restore CLAUDE.md from the base commit to ensure no damage is merged
cd "$DGOV_WORKTREE_PATH"
git checkout "$(git merge-base HEAD main)" -- CLAUDE.md
git add CLAUDE.md
git commit --amend --no-edit || true
```

## Fallback behaviors

If no hook is found, dgov executes default fallback behaviors:

- **`worktree_created`**: Appends a "protected files" warning to the agent's prompt.
- **`pre_merge`**: Automatically restores `CLAUDE.md` and other protected files from the base commit.
- **`post_merge`**: Runs `ruff check --fix` and `ruff format` on all modified Python files.
