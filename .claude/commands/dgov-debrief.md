Debrief a dgov session or failed worker. Analyze what happened and update the ledger.

## Step 1: Gather evidence (run in parallel)

```bash
# Recent events
uv run dgov status -r .

# Recent failures and retries
uv run dgov ledger list -r . -c bug -s open
uv run dgov ledger list -r . -c fix

# Agent stats
uv run dgov agent stats -r . 2>/dev/null || echo "no stats"

# Recent git activity
git log --oneline -20
```

If the user names a specific pane slug, also run:
```bash
uv run dgov pane transcript <slug> 2>/dev/null | tail -50
uv run dgov pane review <slug> 2>/dev/null
```

## Step 2: Analyze patterns

Look for:
- **Repeated failures**: same task failing across retries/escalations — why?
- **Model capabilities**: what did each agent tier handle well vs poorly?
- **Prompt quality**: did vague prompts cause stalls? did numbered steps help?
- **Infrastructure issues**: tunnel failures, GPU saturation, timeout patterns?
- **Policy violations**: did any path skip preflight/review/cleanup?

## Step 3: Update the ledger

For each finding, add the appropriate ledger entry:

```bash
# Bugs discovered
uv run dgov ledger add bug "<description>" -r . -s <severity> -t <tag>

# Patterns observed
uv run dgov ledger add pattern "<description>" -r .

# Model capabilities learned
uv run dgov ledger add capability "<model can/cannot do X>" -r . --status accepted

# Decisions made
uv run dgov ledger add decision "<why we chose X>" -r .

# Fixes applied
uv run dgov ledger add fix "<what was fixed>" -r . --status fixed

# Tech debt identified
uv run dgov ledger add debt "<what needs cleanup>" -r . -s <severity>
```

Resolve any bugs that were fixed this session:
```bash
uv run dgov ledger resolve <id> -s fixed
```

## Step 4: Report summary

Print a compact debrief:
```
Debrief: <date>
  dispatched: N panes (N succeeded, N failed, N retried)
  roles: worker (N tasks, M% pass), supervisor (N tasks, M% pass), manager (N tasks, M% pass)
  routing notes: <logical agent names or backend notes only when evidence makes them relevant>
  patterns: <1-2 line summary of key observations>
  ledger: +N bugs, +N patterns, +N capabilities, N resolved
```

## Rules
- Be specific — "qwen-9b failed on multi-file edit" not "worker failed"
- Capabilities are positive too — record what worked, not just failures
- Resolve fixed bugs immediately, don't let them accumulate
- If a pattern repeats 3+ times, it should become a CLAUDE.md rule — flag it
- Default to role-level reporting; mention physical backends only when the routing detail is part of the failure
