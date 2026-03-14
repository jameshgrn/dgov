# Preflight checks

dgov runs a suite of pre-flight checks before dispatching a worker to ensure your workstation is ready and the repository is clean. This prevents dispatches that are doomed to fail due to environment or configuration issues.

## When preflight runs

- **Automatically**: Every `dgov pane create` command runs pre-flight checks by default.
- **Standalone**: You can run them at any time using `dgov preflight`.
- **Batch**: `dgov batch` runs pre-flight for all tasks in a tier.

## The checks

| Check | Importance | What it tests |
|-------|------------|---------------|
| `agent_cli` | **CRITICAL** | Is the agent's CLI binary on your `PATH`? |
| `git_clean` | **CRITICAL** | Are there uncommitted changes to tracked files? |
| `git_branch`| Warning | Are you on the expected branch (usually `main`)? |
| `tunnel` | **CRITICAL** | Are the SSH tunnel health-checks passing (for `pi`)? |
| `kerberos` | **CRITICAL** | Do you have a valid Kerberos ticket (for `pi`)? |
| `deps` | Warning | Are your dependencies in sync with `pyproject.toml`? |
| `stale_worktrees`| Warning | Are there any git worktrees without matching panes? |
| `file_locks`| **CRITICAL** | Do your `touches` overlap with any active panes? |
| `agent_concurrency`| **CRITICAL** | Will this dispatch exceed the agent's `max_concurrent`? |
| `agent_health`| **CRITICAL** | Does the agent's custom `health_check` command pass? |

## Standalone usage

Run checks for a specific agent and project:

```bash
# Run checks and attempt to fix failures
dgov preflight -a pi -r . --fix

# Check for file conflicts specifically
dgov preflight -a claude -t src/parser.py -t src/models.py
```

## Auto-fix

When the `--fix` flag is set (on by default during `pane create`), dgov will attempt to resolve certain failures automatically:

- **`tunnel`**: Restarts the SSH tunnel.
- **`kerberos`**: Runs `kinit`.
- **`deps`**: Runs `uv sync`.
- **`stale_worktrees`**: Runs `git worktree prune`.
- **`agent_health`**: Runs the agent's `health_fix` command.

If an auto-fix succeeds, dgov re-runs the failed check to confirm the problem is resolved.

## Disabling checks

If you are confident in your environment or running in a context where some checks (like Kerberos or SSH tunnels) are not applicable, use the `--no-preflight` flag.

```bash
dgov pane create -a claude -p "..." --no-preflight
```

## Output format

The standalone command outputs a structured JSON report:

```json
{
  "checks": [
    {
      "name": "agent_cli",
      "passed": true,
      "critical": true,
      "message": "claude found on PATH",
      "fixable": false
    },
    ...
  ],
  "passed": true,
  "timestamp": "2026-03-12T18:22:10Z"
}
```
