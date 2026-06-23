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

## Do
- keep `kernel.py` pure: `(state, event) -> (new_state, actions)` with no I/O or persistence imports
- prefer derivation from durable evidence like events over storing redundant booleans or cached conclusions
- prefer explicit state machines or discriminated models over optional-field grab bags
- remove dead variants or impossible branches when they no longer represent real lifecycle states
- make broader state-model reshapes explicit in the task when the model itself is wrong

## Do Not
- pile on flags to explain one condition
- preserve contradictory or impossible states just to keep a diff smaller

## Verify
- check that structural invariants still fail closed at compile or settlement time
- confirm changed models have a single, unambiguous lifecycle story
- run targeted tests that exercise the affected state transitions or event handling

## Escalate
- if fixing the model would broaden the task beyond its current file claims or plan scope
- if a proposed state shape changes public interfaces or persistence semantics

## Lacustrine Pillars

Preserve the relevant Lacustrine Pillar headers when editing boundary modules.

### When
- editing runner, worker, settlement, worktree, plan, or persistence boundaries
- refactoring code that touches module-level docstrings or file headers

### Do
- keep `Follows Lacustrine Pillars:` headers and `Pillar #N:` bullet lines intact in boundary files
- confirm the file still satisfies the constraint named in each pillar comment after your change
- flag any pillar whose constraint has become stale or is violated

### Do Not
- remove or reword pillar comments without a governance repair plan
- add new pillars without updating `.dgov/governor.md` as the canonical source

### Verify
- `uv run pytest -q -m unit tests/test_boundaries.py` confirms pillar markers remain visible
