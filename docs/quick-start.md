# Quick start

This guide gets you from zero to a merged change using `dgov`.

## 1. Start a dgov session

Run `dgov` from your main repository root. If you're not inside tmux, it will create a session and attach.

```bash
# From main repository root
dgov
```

## 2. Dispatch a worker

Create a worker pane to perform a task. Use `-a` for the agent and `-p` for the prompt.

```bash
# Add a simple health check
dgov pane create -a claude -p "Add a health check endpoint to app.py" -r .
```

You'll see a JSON response with the **slug** (e.g., `add-health-check`), which identifies the task.

## 3. Wait for completion

dgov dispatches the task into a separate tmux pane and git worktree. Wait for the worker to finish:

```bash
# Block until done
dgov pane wait add-health-check
```

## 4. Review the diff

Before merging, see what the agent actually did:

```bash
# Show diff summary and safety verdict
dgov pane review add-health-check

# Or see the full diff
dgov pane diff add-health-check
```

## 5. Merge and close

If the changes look good, merge the worker branch into `main` and clean up:

```bash
# Merge into main and remove the worker worktree
dgov pane merge add-health-check
```

## 6. Verify status

See the current state of all panes:

```bash
dgov pane list
dgov status
```

---

## Full workflow summary

```bash
# Start
dgov

# Dispatch
dgov pane create -a claude -p "Refactor parser.py to use match statement"

# Wait
dgov pane wait refactor-parser

# Review
dgov pane review refactor-parser

# Merge
dgov pane merge refactor-parser
```
