"""Project root resolution helpers."""

from __future__ import annotations

from pathlib import Path


class ProjectPathError(ValueError):
    """Raised when an operator-supplied path escapes the project root."""


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


def resolve_project_path(
    project_root: str | Path, path: str | Path, *, label: str = "path"
) -> Path:
    """Resolve an operator-supplied path and require it to stay inside project_root."""
    root = Path(project_root).resolve()
    raw_path = Path(path)
    candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProjectPathError(f"{label} must stay under project root {root}: {raw_path}") from exc

    return resolved
