# Quick start

This guide gets you from zero to a merged change using `dgov`.

## 1. Start a dgov session

Run `dgov` from your main repository root. If you're not inside tmux, it will create a session and attach.

```bash
# From main repository root
dgov
```

## 2. Create a plan

Write a plan TOML file describing the task:

```toml
name = "add-health-check"
description = "Add a health check endpoint to app.py"
agent = "claude"
prompt = "Add a /health endpoint to app.py that returns 200 OK"
```

Save this to `.dgov/plans/health-check.toml`.

## 3. Run the plan

Execute the plan through the full pipeline (dispatch → review → merge):

```bash
dgov plan run .dgov/plans/health-check.toml
```

You'll see a JSON response with the **slug** (e.g., `add-health-check`), which identifies the task.

## 4. Monitor in the dashboard

Open the TUI dashboard to monitor all active panes:

```bash
dgov dashboard
```

## 5. Review the diff

Before the kernel merges, you can inspect what the agent did:

```bash
# Show diff summary and safety verdict
dgov pane review add-health-check

# Or see the full diff
dgov pane diff add-health-check
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

# Create a plan file
cat > .dgov/plans/refactor-parser.toml << 'EOF'
name = "refactor-parser"
description = "Refactor parser to use match statement"
agent = "claude"
prompt = "Refactor parser.py to use match statement instead of if/elif chains"
EOF

# Run the plan (dispatch → review → merge)
dgov plan run .dgov/plans/refactor-parser.toml

# Monitor in dashboard
dgov dashboard  # (optional, runs in background)

# Review if needed
dgov pane review refactor-parser
```
