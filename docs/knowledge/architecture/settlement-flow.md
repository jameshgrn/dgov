---
id: settlement-flow
title: Settlement Flow
kind: architecture
status: living
sources:
  - .dgov/governor.md
  - src/dgov/runner.py
  - src/dgov/settlement.py
  - src/dgov/settlement_flow.py
related:
  - sentrux
  - failure-shapes
  - ledger
---

# Settlement Flow

Settlement is the boundary between worker output and accepted repository
state. Workers can be probabilistic; settlement must be deterministic.

The flow checks that a candidate change stays inside declared file claims,
passes configured verification, respects scope ignores, and does not degrade
structural quality. This is why plans must claim files precisely: settlement
cannot safely infer that an unclaimed edit was intentional.

The flow also separates worker-task completion from governor finalization. A
worker can finish its patch while archiving, bookkeeping, or structural checks
still need attention. The governor charter's failure catalog exists so those
post-worker failures map to specific next actions instead of vague retries.

Settlement should consume public policy and diagnostic helpers rather than
reaching across layers for private implementation details. That keeps the gate
auditable and prevents hidden coupling between orchestration, persistence, and
semantic checks.
