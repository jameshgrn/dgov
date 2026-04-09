---
name: testing
title: Testing Conventions
summary: Testing rules for targeted execution, scope discipline, and behavior-first verification.
applies_to: [tests, pytest, verification]
priority: must
---
## When
- writing or editing tests
- choosing verification commands after a code change
- investigating test failures during worker verification

## Do
- use `pytest -q`
- run targeted subsets with markers like `-m unit` or `-m integration`
- test behavior and edge cases, not just implementation details
- mock boundaries only: network, filesystem, and external services
- confirm a relevant test would fail before claiming the fix is real

## Do Not
- run the full test suite
- edit tests in unclaimed files
- mock logic or internal state just to force a passing result

## Verify
- rerun the exact targeted command that covers the changed behavior
- report failing tests in unclaimed files instead of editing them
- confirm scope violations are avoided before settlement

## Escalate
- if the right test file is outside the current claim
- if the change needs broader integration coverage than the task currently allows
