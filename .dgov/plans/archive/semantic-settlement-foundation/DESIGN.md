# Semantic Settlement Foundation

## Goal

Move `dgov` from task-local validation plus Git-native landing toward
integration-aware settlement, without jumping straight to automatic merge
synthesis.

The first branch is deliberately narrower than "semantic merge":

1. define the contract and failure taxonomy
2. measure real integration risk in shadow mode
3. validate an integrated candidate before landing
4. add deterministic Python semantic gates
5. keep synthesis out of scope until the earlier stages prove useful

## Current seam

Today the runtime split is clear:

- isolated task work happens in git worktrees
- task-local validation lives in `src/dgov/settlement.py`
- landing happens through `src/dgov/worktree.py::merge_worktree`
- orchestration happens in `src/dgov/runner.py::_settle_and_merge`

That means the right insertion point is between isolated validation and final
merge, not a wholesale rewrite of settlement.

## Failure taxonomy

This branch should make these classes explicit and machine-readable:

- `text_conflict`: Git cannot replay the task commit cleanly
- `syntax_conflict`: the integrated file no longer parses
- `same_symbol_edit`: both sides changed the same Python symbol
- `duplicate_definition`: integrated Python code defines the same symbol twice
- `signature_drift`: a touched public callable changed shape relative to the
  task snapshot or target head
- `ordering_conflict`: operations are individually valid but invalid in the
  integrated order
- `behavioral_mismatch`: integrated candidate passes parse-level checks but
  fails settlement gates

The first branch only needs deterministic detection and clear evidence. It does
not need automatic resolution.

## Dogfooding phases

### Phase 1: Contract and telemetry schema

Create a dedicated semantic-settlement module with explicit risk and verdict
types plus event payload helpers. Extend the persistence event schema so the
runtime can emit integration telemetry without inventing ad hoc blobs.

Exit criteria:

- event names and payload shapes are stable and tested
- no merge behavior changes yet
- review tooling can rely on the new event family existing

### Phase 2: Shadow-mode risk scoring

Before landing a task, compute an integration risk record using:

- current target `HEAD`
- task base snapshot
- task commit diff
- declared file claims
- changed files
- lightweight Python symbol overlap when relevant

Emit telemetry only. Do not block merge yet.

Exit criteria:

- every merged task can carry a risk record
- false positives and real catches can be inspected in `dgov plan review`
- no change to success/failure behavior

### Phase 3: Integrated candidate validation

Build an ephemeral candidate workspace rooted at current target `HEAD`, replay
the task commit onto it, and run the normal settlement gates against that
integrated result before final landing.

Exit criteria:

- integrated candidate pass/fail is evented and reviewable
- the main repo stays clean on replay failure
- isolated-green but integrated-bad changes are rejected before landing

### Phase 4: Deterministic Python semantic gates

On top of the integrated candidate, add Python-only checks for:

- same-symbol concurrent edits
- duplicate definitions
- public signature drift in touched modules

These checks are deterministic and reject with evidence. They do not rewrite
code and do not try to merge ASTs.

Exit criteria:

- non-Python tasks remain unaffected
- failing gates emit precise reasons
- targeted tests cover collisions, duplicates, and signature drift

## Event model

The branch should converge on this event family:

- `integration_risk_scored`
- `integration_overlap_detected`
- `integration_candidate_passed`
- `integration_candidate_failed`
- `semantic_gate_rejected`

Each event should be machine-readable enough for `plan_review.py` to build a
per-unit summary without re-deriving integration state from Git after the fact.

## Non-goals

This branch must NOT:

- introduce automatic semantic merge synthesis
- add new public CLI flags for settlement policy
- replace Git as the landing primitive
- create domain adapters beyond Python
- broaden settlement into a second orchestration layer

## Promotion rule

Nothing in this branch becomes default-on blocking behavior until the prior
phase has been dogfooded on normal `dgov` work and the evidence looks better
than the baseline.
