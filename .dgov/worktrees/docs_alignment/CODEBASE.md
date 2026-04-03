# CODEBASE

## INVARIANTS
- Event-driven, no polling. State derives from events.
- One canonical dispatch path: preflight → build → spawn → track.
- No compatibility shims. Hard-cut ownership.

## MODULES

[core orchestration]
  src/dgov/pane.py — Pane lifecycle: create, track, complete, cleanup.
  src/dgov/runner.py — Execution runner: preflight, dispatch, wait.
  src/dgov/kernel.py — Kernel primitives for pane and DAG execution.
  src/dgov/done.py — Done-signal detection and exit code handling.
  src/dgov/observation.py — Unified worker state classification.
  src/dgov/status.py — Pane status queries and output capture.

[merge/review]
  src/dgov/worktree.py — Worktree management and git isolation.
  src/dgov/unit.py — Merge unit: validation, apply, cleanup.
  src/dgov/unit_compile.py — Unit compilation from events.

[persistence]
  src/dgov/persistence.py — Persistence layer facade.
  src/dgov/persistence/connection.py — DB connection management.
  src/dgov/persistence/events.py — Event log operations.
  src/dgov/persistence/schema.py — Schema definitions.
  src/dgov/persistence/state_ops.py — Pane state CRUD.

[agent integration]
  src/dgov/agents.py — Agent registry and launch commands.
  src/dgov/router.py — (kind, tier) routing: resolve() maps logical agents to concrete backends.
  src/dgov/strategy.py — Commit message inference and file extraction from prompts.

[monitor/recovery]
  src/dgov/monitor.py — Event-driven monitor daemon.
  src/dgov/monitor_hooks.py — Configurable monitor actions.

[DAG/plans]
  src/dgov/dag_parser.py — DAG TOML parser.
  src/dgov/cli/plan.py — Plan execution CLI.

[decisions]
  (DecisionKind, CapabilityTier) routing matrix defined in CLAUDE.md.
  Router resolve() enforces deterministic backend selection.

[reporting]
  src/dgov/spans.py — Structured observability spans.
  src/dgov/cost_tracker.py — Cost tracking.
  src/dgov/ledger_store.py — Operational ledger storage.
  src/dgov/dashboard.py — Live TUI dashboard.

[backend]
  src/dgov/backend.py — Worker backend interface.
  src/dgov/tmux.py — tmux command wrappers.

[CLI]
  src/dgov/cli/__init__.py — CLI entry point.
  src/dgov/cli/pane.py — Pane commands.
  src/dgov/cli/plan.py — Plan commands.
  src/dgov/cli/run.py — Run commands.
  src/dgov/cli/status.py — Status commands.
  src/dgov/cli/config.py — Config commands.
  src/dgov/cli/ledger.py — Ledger commands.

[terrain]
  src/dgov/terrain/__init__.py
  src/dgov/terrain/effects.py
  src/dgov/terrain/model.py
  src/dgov/terrain/phases.py
  src/dgov/terrain/render.py

[types]
  src/dgov/types.py — Core type definitions.
  src/dgov/config.py — Configuration loader.

## TESTS (actual)
  tests/test_router.py
  tests/test_architecture.py
  tests/test_cost_tracker.py
  tests/test_kernel_conflict.py
  tests/test_integration_conflict.py
  tests/test_latency.py
  tests/benchmark_dispatch.py
  tests/test_dogfood_routed_events.py — Routed dispatch verification by event truth.
