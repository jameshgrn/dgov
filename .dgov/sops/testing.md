---
name: testing
title: Testing Conventions
---
## Execution
- **`pytest -q`:** Always run tests in quiet mode.
- **Markers:** Never run the full test suite. Use `-m unit` or `-m integration` to target the right subset.
- **Unit vs Integration:** `unit` tests have no external deps; `integration` tests use real git repos and temp directories.

## Methodology
- **Behavior over Implementation:** Test what the code does, not how it's structured.
- **Edge Cases:** Prioritize testing edges and error paths, not just the happy path.
- **Mocking:** Mock boundaries only (network, filesystem, external services). Never mock logic or internal state.
- **Verification:** Break the code to confirm the test fails before fixing it.
