"""Project root resolution helpers."""

from __future__ import annotations

from pathlib import Path


def resolve_project_root(start: str | Path | None = None) -> Path:
    """Resolve the repository root from any path inside the repo.

    If called from within `.dgov/` or one of its subdirectories, returns the
    parent project root rather than the literal current directory.
    """
    current = Path.cwd() if start is None else Path(start)
    current = current.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if candidate.name == ".dgov":
            return candidate.parent
        if (candidate / ".dgov").is_dir():
            return candidate
        if (candidate / ".git").exists():
            return candidate

    return current
