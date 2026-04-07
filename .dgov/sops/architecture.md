---
name: architecture
title: System Architecture & State Management
---
## Core Principles
- **Separation of Powers:** The Plan is the contract between Governor and Worker.
- **Pure Kernel:** `kernel.py` is a pure function: `(state, event) -> (new_state, actions)`. No I/O or persistence imports.
- **Event-Sourced:** All state transitions are logged to SQLite via the runner async bridge.
- **Worker Isolation:** Workers run in isolated git worktrees on their own branches. No shared mutable state.
- **Settlement:** Commit-or-kill pipeline. Changes only land if they pass ruff lint + sentrux policy gates.

## Determinism
- Rejects cycles, unreachable units, and invalid TOML schemas immediately at compile time.
- All structural invariants are validated before execution starts.
