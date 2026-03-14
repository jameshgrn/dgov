# Troubleshooting

Common issues and how to resolve them.

## General issues

**"dgov is running inside a git worktree"**
You are trying to run the governor from a worker's worktree. Change your directory back to the main repository root. If you are doing development on dgov itself, set `DGOV_SKIP_GOVERNOR_CHECK=1`.

**"Governor is on branch X, but must stay on main"**
The governor dispatches and merges from the `main` branch. Use `git checkout main` before running dgov.

**Worker pane is stuck as "active" forever**
- Use `dgov pane nudge <slug>` to ask the worker if it is done.
- Use `dgov pane capture <slug>` to see if it's waiting for input.
- If it's truly stuck, use `dgov pane signal <slug> done` (if changes are made) or `dgov pane close <slug>` (to discard).

**Agent not found**
Run `dgov agents` to see if the CLI is detected on your `PATH`. Ensure the agent binary is installed (e.g., `npm install -g @anthropic-ai/claude-code`).

**State database locked**
dgov uses SQLite in WAL mode, but if multiple processes are stuck, you can safely delete `.dgov/state.db-wal` and `.dgov/state.db-shm` **only if no dgov processes are running**.

## Git and worktree issues

**Stale worktrees**
If you manually delete worktrees or tmux panes, `dgov pane list` might show stale entries. Run `dgov pane prune` to cleanup the database and `dgov preflight --fix` to prune git worktrees.

**Merge conflicts**
dgov tries to auto-resolve conflicts with an agent. If this fails, use `dgov pane merge --resolve manual` and resolve them in your editor.

**Protected files clobbered**
Workers (like `claude`) often overwrite `CLAUDE.md`. This is expected. dgov's `pre_merge` step automatically restores these files before the merge happens.

## Tmux issues

**"not a terminal" or "failed to create pane"**
Ensure you are running dgov **inside** a tmux session. If you're using Ghostty, you may need to force `default-terminal "tmux-256color"` in your `.tmux.conf`.

**VIRTUAL_ENV leaks into worker**
If the worker is using the wrong Python version, it might be inheriting your `VIRTUAL_ENV`. Unset the variable before launching dgov or use the `--env` flag to override it.

## Agent-specific issues

**`pi` worker doesn't commit**
The `pi` agent requires explicit instructions to commit. Use the `bugfix` or `feature` templates, which include `git commit` in the prompt.

**`pi` fails preflight for tunnel/kerberos**
The `pi` agent requires an SSH tunnel to a GPU cluster. If you aren't using `pi`, you can ignore these warnings or use `--no-preflight`. If you are using `pi`, run `dgov preflight --fix` to restart the tunnel and renew your credentials.
