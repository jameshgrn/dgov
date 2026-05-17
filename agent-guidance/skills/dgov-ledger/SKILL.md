---
name: dgov-ledger
description: |
  Knowledge operations — record bugs, rules, patterns, decisions, debt, notes,
  capabilities, and fixes. Query at session start when durable context matters;
  write when a learned fact should outlive the session.
author: Jake Gearon
version: 6.0.0
date: 2026-05-17
---

# dgov-ledger — Structured Knowledge Store

The ledger is durable operational memory. It is the right place for learned
bugs, rules, decisions, recurring patterns, debt, capabilities, fixes, and
notes. It is not a scratchpad for transient session state.

## Categories

| Category | Purpose | Example |
|----------|---------|---------|
| `bug` | Track active issues | Parser fails on nested quotes |
| `fix` | Record what was fixed | Fixed race in worker cleanup |
| `rule` | Hard-won invariants | Never bypass file-claim settlement |
| `pattern` | Recurring observations | Small workers handle one-file edits well |
| `note` | Durable contextual note | Release checklist lives in docs/release.md |
| `debt` | Tech debt to address | Duplicate retry logic in monitor |
| `capability` | Model/provider capabilities | model-x: reliable for single-file edits |
| `decision` | Why we chose X | Use plans over ad hoc dispatch |

## Commands

```bash
# Query
uv run dgov ledger list -r . -c bug -s open
uv run dgov ledger list -r . -c rule
uv run dgov ledger list -r . -c debt -s open
uv run dgov ledger list -r . -q "scope"

# Write
uv run dgov ledger add bug "Parser fails on nested quotes" -r . --path src/parser.py
uv run dgov ledger add pattern "One-file edits settle cleanly with explicit prompts" -r .
uv run dgov ledger add decision "Use plans over ad hoc dispatch" -r .

# Resolve
uv run dgov ledger resolve <id> -r .
```

## CLI Surface

Trust `uv run dgov ledger --help` and subcommand help over older examples.
The installed CLI supports:

- categories: `bug`, `fix`, `rule`, `pattern`, `note`, `debt`, `capability`, `decision`
- statuses: `open`, `resolved`
- `--path` on `ledger add`, repeatable for affected paths
- `--query` on `ledger list`

The installed CLI does not expose severity flags, tags, custom status values,
or `dgov ledger show`.

## Session Workflow

At start, only query the ledger when prior bugs, rules, decisions, or debt
could affect the task:

```bash
uv run dgov ledger list -r . -c bug -s open
uv run dgov ledger list -r . -c rule
```

During work, write durable findings as soon as they become stable:

```bash
uv run dgov ledger add rule "New invariant learned" -r .
uv run dgov ledger add bug "Found edge case in plan compilation" -r . --path src/dgov/plan.py
```

At handover or closeout, verify open bugs if the session found or resolved
runtime issues:

```bash
uv run dgov ledger list -r . -c bug -s open
```
