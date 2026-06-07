"""Plan source discovery for operator-facing status views."""

from __future__ import annotations

from pathlib import Path


def active_plan_source_names(project_root: str | Path) -> frozenset[str]:
    """Return names of active plan source directories in this checkout."""
    root = Path(project_root)
    names: set[str] = set()
    for plans_dir in (
        root / ".dgov" / "plans",
        root / ".dgov" / "runtime" / "fix-plans",
    ):
        names.update(_plan_names_in(plans_dir))
    return frozenset(names)


def _plan_names_in(plans_dir: Path) -> set[str]:
    if not plans_dir.is_dir():
        return set()
    names: set[str] = set()
    for child in plans_dir.iterdir():
        if child.name == "archive" or not child.is_dir():
            continue
        if (child / "_root.toml").exists() or (child / "_compiled.toml").exists():
            names.add(child.name)
    return names
