---
id: failure-shapes
title: Failure Shapes
kind: operation
status: living
sources:
  - .dgov/governor.md
  - src/dgov/diagnose.py
  - src/dgov/cli/diagnose.py
related:
  - settlement-flow
  - sentrux
  - ledger
---

# Failure Shapes

A failure shape is observable evidence mapped to a typed next task. The point
is to keep recovery deterministic when a run fails after worker execution has
already done useful work.

The governor charter is the human-readable catalog. `dgov diagnose` is the
mechanical surface for failure shapes that can be detected from live repo
state. Not every shape has a mechanical signal, so diagnosis is a filter, not a
replacement for governor judgment.

Good failure-shape entries include:

- evidence that can be checked
- the class of work to perform next
- the smallest corrective action
- an explicit "do not" that blocks common wrong retries

When a ledger rule or pattern describes a recurring failure shape, the charter
catalog should be updated in the same change. Otherwise durable memory and
operator workflow drift apart.
