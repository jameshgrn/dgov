---
name: dgov-pane
description: |
  Historical pane lifecycle notes. The installed dgov CLI no longer exposes
  pane commands; use dgov plan and dgov fix instead.
author: Jake Gearon
version: 6.0.0
date: 2026-05-17
---

# dgov-pane — Retired Micro-Task Surface

The installed dgov CLI does not provide `dgov pane ...` commands. Treat any
pane command in older notes, handovers, or prompts as stale guidance.

## Use These Commands Instead

| Situation | Command |
|-----------|---------|
| One-off scoped fix | `uv run dgov fix "<prompt>" --file <path>` |
| New plan tree | `uv run dgov init-plan <name>` |
| Compile a plan | `uv run dgov compile .dgov/plans/<name>` |
| Validate a plan | `uv run dgov validate .dgov/plans/<name>` |
| Run a plan | `uv run dgov run .dgov/plans/<name>` |
| Continue a plan | `uv run dgov run --continue .dgov/plans/<name>` |
| Inspect a plan | `uv run dgov plan review .dgov/plans/<name>` |
| Check deployment state | `uv run dgov plan status .dgov/plans/<name>` |
| Watch live events | `uv run dgov watch` |

Before relying on historical command text, run:

```bash
uv run dgov --help
```

Trust the installed CLI surface over stale skill text or handover snippets.

## Anti-Patterns

- Do not run `uv run dgov pane create`, `pane wait`, `pane land`,
  `pane review`, `pane close`, or `pane transcript`.
- Do not describe pane as the active dispatch surface.
- Do not translate old pane prompts literally; convert them into plan tasks
  with explicit file claims and Orient / Edit / Verify prompts.
