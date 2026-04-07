---
name: python-style
title: Python Toolchain & Code Style
---
## Toolchain
- **uv over pip/poetry:** Always use `uv run <command>` prefix for Python tools in uv-managed projects.
- **Ruff over black/pylint/flake8:** Use `ruff format` and `ruff check`.
- **Ty Check:** Run `ty check` over mypy/pyright.
- **Zero Warnings:** Fix every warning from linters, type checkers, and tests.

## Style Rules
- **No Commented-Out Code:** Delete it; don't leave it in.
- **Minimal Edits:** When editing an existing file, change only the lines relevant to your task. Never rewrite unchanged code. If your task says "change the import block", change only the import lines — do not touch anything else in the file.
- **Minimal Annotations:** No docstrings, comments, or type annotations on code you didn't change.
- **Explicit Logic:** Clarity over cleverness. Explicit readable code over dense one-liners.
- **No Premature Abstraction:** Don't create utilities until the same code has been written 3x.
- **Self-Evident Code:** Only add comments where logic isn't self-evident.
