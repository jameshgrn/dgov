---
name: dgov
description: |
  DEPRECATED: Use dgov-bootstrap instead.
  This skill is kept for backward compatibility. New sessions should use
  the function-based skills: dgov-bootstrap, dgov-plan, dgov-pane, dgov-ledger.
author: Jake Gearon
version: 4.1.1-deprecated
date: 2026-03-29
---

# DEPRECATED: dgov

**This skill has been reorganized. Use the function-based skills instead:**

| Old | New | Purpose |
|-----|-----|---------|
| `dgov` (this) | `dgov-bootstrap` | Session start, readiness checks |
| `dgov-governor` | `dgov-plan` | Plan operations (primary dispatch) |
| `dgov-governor` | `dgov-pane` | Pane lifecycle (micro-tasks) |
| (implicit) | `dgov-ledger` | Knowledge operations |
| `dgov-handover` | `dgov-handover` | Session end (unchanged) |

## Migration

If you see this skill, you're using an old configuration. Update your skills path to use the new function-based organization.
