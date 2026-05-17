---
id: ledger
title: Ledger
kind: operation
status: living
sources:
  - .dgov/governor.md
  - src/dgov/cli/ledger.py
  - src/dgov/persistence/ledger.py
related:
  - knowledge-pull-architecture
  - failure-shapes
  - settlement-flow
---

# Ledger

The ledger is dgov's durable operational memory. It stores bugs, fixes, rules,
patterns, decisions, debt, and capability notes that need to survive beyond a
single session.

The ledger is intentionally different from the knowledge base. Ledger entries
are structured memory and can represent active work. Knowledge articles are
curated explanations derived from canonical sources. If an article records a
new rule or recurring bug directly, it is probably hiding durable memory in the
wrong place.

The governor charter treats the ledger as the first place to query when prior
bugs, rules, or decisions matter. When a ledger rule becomes operational law,
the relevant charter or SOP should be updated so workers and governors see the
rule during normal execution.
