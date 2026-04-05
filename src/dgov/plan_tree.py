"""Plan tree walker — phase 1 of the compile pipeline.

Reads `_root.toml` + section directories into a PlanTree.
See .dgov/plans/plan-system/DESIGN.md for the full compile pipeline.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RootMeta:
    """Parsed `[plan]` section of `_root.toml`."""

    name: str
    summary: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class PlanTree:
    """Result of walking a plan tree (depth-1, flat per section)."""

    plan_root: Path
    root_meta: RootMeta
    section_files: dict[str, tuple[Path, ...]]


def walk_tree(plan_root: Path) -> PlanTree:
    """Walk a plan tree rooted at plan_root.

    Reads `_root.toml`, enumerates declared sections, collects depth-1 `*.toml`
    files per section (excluding hidden and `_`-prefixed files, excluding
    subdirectories).

    Raises:
        FileNotFoundError: `_root.toml` missing.
        ValueError: invalid metadata, or declared section missing its directory.
    """
    root_file = plan_root / "_root.toml"
    if not root_file.exists():
        raise FileNotFoundError(f"_root.toml not found in {plan_root}")

    raw = tomllib.loads(root_file.read_text())
    plan_section = raw.get("plan")
    if not plan_section:
        raise ValueError(f"_root.toml missing [plan] section: {root_file}")

    name = plan_section.get("name")
    if not name:
        raise ValueError(f"_root.toml [plan] missing 'name': {root_file}")
    summary = plan_section.get("summary", "")
    sections = plan_section.get("sections", [])

    if not isinstance(sections, list):
        raise ValueError(f"_root.toml [plan].sections must be a list: {root_file}")
    if not all(isinstance(s, str) for s in sections):
        raise ValueError(f"_root.toml [plan].sections must contain only strings: {root_file}")

    section_files: dict[str, tuple[Path, ...]] = {}
    for section in sections:
        section_dir = plan_root / section
        if not section_dir.is_dir():
            raise ValueError(f"Declared section '{section}' has no directory at {section_dir}")
        files = sorted(
            p
            for p in section_dir.iterdir()
            if p.is_file()
            and p.suffix == ".toml"
            and not p.name.startswith(".")
            and not p.name.startswith("_")
        )
        section_files[section] = tuple(files)

    return PlanTree(
        plan_root=plan_root,
        root_meta=RootMeta(name=name, summary=summary, sections=tuple(sections)),
        section_files=section_files,
    )
