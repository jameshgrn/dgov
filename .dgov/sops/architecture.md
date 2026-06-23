---
name: architecture
title: System Architecture & State Management
summary: Architectural invariants, state-modeling rules, and deterministic system boundaries.
applies_to: [architecture, state, kernel, model, layer, boundary, import, coupling, runner, settlement]
priority: must
---
## When
- changing state types, event models, or lifecycle transitions
- editing kernel, runner, persistence, or other orchestration boundaries
- refactoring code that currently relies on multiple booleans or optional-field grab bags
- preserving Lacustrine Pillar boundaries in runner, worktree, worker,
  settlement, plan, or persistence code

## Do
- keep `kernel.py` pure: `(state, event) -> (new_state, actions)` with no I/O or persistence imports
- prefer derivation from durable evidence like events over storing redundant booleans or cached conclusions
- prefer explicit state machines or discriminated models over optional-field grab bags
- remove dead variants or impossible branches when they no longer represent real lifecycle states
- make broader state-model reshapes explicit in the task when the model itself is wrong
- preserve `Follows Lacustrine Pillars:` headers and `Pillar #N:` bullet lines
  in boundary files
- treat the visible Lacustrine Pillars as policy constraints: Pillar #1
  Separation of Powers, Pillar #2 Atomic Attempt, Pillar #3 Snapshot Isolation,
  Pillar #4 Determinism, Pillar #6 Event-Sourced, Pillar #7 Zero Ambient
  Authority, Pillar #8 Falsifiable Validation, Pillar #9 Hot-Path, and Pillar
  #10 Fail-Closed

## Do Not
- pile on flags to explain one condition
- preserve contradictory or impossible states just to keep a diff smaller
- remove or reword Lacustrine Pillar markers without a governance repair plan
- add new pillar definitions without updating `.dgov/governor.md` as the
  canonical source

## Verify
- check that structural invariants still fail closed at compile or settlement time
- confirm changed models have a single, unambiguous lifecycle story
- run targeted tests that exercise the affected state transitions or event handling
- run `uv run pytest -q -m unit tests/test_boundaries.py` when changing source
  files or policy surfaces that carry Lacustrine Pillar markers

## Escalate
- if fixing the model would broaden the task beyond its current file claims or plan scope
- if a proposed state shape changes public interfaces or persistence semantics
- if adding or renumbering a Lacustrine Pillar, especially the currently absent
  Pillar #5 definition, requires a broader policy decision
