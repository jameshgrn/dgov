---
id: sentrux
title: Sentrux
kind: concept
status: living
sources:
  - .dgov/governor.md
  - src/dgov/cli/sentrux.py
  - src/dgov/sentrux_gate.py
  - src/dgov/sentrux_baseline.py
related:
  - settlement-flow
  - failure-shapes
  - knowledge-pull-architecture
---

# Sentrux

Sentrux is dgov's architectural sensing layer. It scores structural quality and
feeds that signal into run, review, and remediation flows.

dgov treats Sentrux output as a governance signal, not as a standalone oracle.
The governor charter names baseline drift as a known failure shape: a stale
baseline can make unrelated work look like a degradation. The intended response
is to refresh the baseline only when the comparison is clean, or to repair real
coupling when the current diff introduces it.

The CLI surface exposes four operator actions:

- `dgov sentrux check`: run an architectural quality scan
- `dgov sentrux gate`: compare the current tree against the saved baseline
- `dgov sentrux gate-save`: save or refresh baseline metadata
- `dgov sentrux offenders`: list likely long or complex function offenders

The important design rule is attribution. A structural warning should tell the
governor whether the current change caused the problem, whether the baseline is
stale, or whether the issue is pre-existing architecture debt.
