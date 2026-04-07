---
name: error-handling
title: Error Handling & Philosophy
---
## Error Handling
- **Fail Fast:** Fail as early as possible with clear, actionable messages.
- **No Silent Failures:** Never swallow exceptions or errors silently.
- **Context:** Always include the operation, input, and a suggested fix in error messages.

## Philosophy
- **Replace, Don't Deprecate:** Completely remove old code entirely; do not leave shims.
- **No Speculative Features:** Don't add features or flags unless actively needed.
- **Bias toward Action:** Decide and move for reversible things; ask for confirmation on data models or architecture.
- **Explicit over Implicit:** Explicit, readable code beats dense, clever one-liners.
