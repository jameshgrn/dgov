"""Plan tree walker + merger — phases 1-2 of the compile pipeline.

Walker reads `_root.toml` + section directories into a PlanTree.
Merger flattens the tree into a FlatPlan with path-qualified unit IDs.
See .dgov/plans/plan-system/DESIGN.md for the full compile pipeline.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dgov.plan import PlanUnit, PlanUnitFiles

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


@dataclass(frozen=True)
class FlatPlan:
    """Result of merging a PlanTree into path-qualified units.

    `depends_on` values on units are raw strings from source TOMLs (bare or
    path-qualified). The resolver rewrites them to fq_ids in a later stage.
    """

    plan_root: Path
    root_meta: RootMeta
    units: dict[str, PlanUnit]  # fq_id -> unit
    source_map: dict[str, Path]  # fq_id -> source TOML path
    source_mtime_max: float


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


def merge_tree(tree: PlanTree) -> FlatPlan:
    """Merge a PlanTree into a FlatPlan with path-qualified unit IDs.

    For each child TOML file, parse `[tasks.*]` tables. Each task's bare slug
    is validated against the slug grammar. The fully-qualified ID is
    `<section>/<file-stem>.<bare-slug>`.

    `depends_on` values are preserved as-is from source (resolver rewrites them).
    Within-file slug duplicates are impossible — tomllib rejects duplicate
    tables at parse time, before this function sees them.

    Raises:
        ValueError: invalid slug grammar, malformed task table, or bad field type.
    """
    units: dict[str, PlanUnit] = {}
    source_map: dict[str, Path] = {}
    mtime_max = 0.0

    for section, toml_paths in tree.section_files.items():
        for toml_path in toml_paths:
            mtime_max = max(mtime_max, toml_path.stat().st_mtime)
            file_stem = toml_path.stem
            raw = tomllib.loads(toml_path.read_text())
            tasks = raw.get("tasks", {})
            for bare_slug, task_data in tasks.items():
                if not _SLUG_RE.match(bare_slug):
                    raise ValueError(
                        f"Invalid slug {bare_slug!r} in {toml_path}: must match {_SLUG_RE.pattern}"
                    )
                if not isinstance(task_data, dict):
                    raise ValueError(f"[tasks.{bare_slug}] must be a table in {toml_path}")
                fq_id = f"{section}/{file_stem}.{bare_slug}"
                units[fq_id] = _unit_from_task(fq_id, task_data, toml_path)
                source_map[fq_id] = toml_path

    return FlatPlan(
        plan_root=tree.plan_root,
        root_meta=tree.root_meta,
        units=units,
        source_map=source_map,
        source_mtime_max=mtime_max,
    )


def _str_list(value: Any, field: str, context: Path) -> tuple[str, ...]:
    """Coerce a TOML value to a tuple of strings, rejecting bad types."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} must be a list of strings in {context}")
    return tuple(value)


def _unit_from_task(fq_id: str, data: dict[str, Any], source: Path) -> PlanUnit:
    """Build a PlanUnit from a parsed `[tasks.<slug>]` table."""
    files_data = data.get("files", {})
    if not isinstance(files_data, dict):
        raise ValueError(f"[tasks.*].files must be a table in {source}")
    files = PlanUnitFiles(
        create=_str_list(files_data.get("create", []), "files.create", source),
        edit=_str_list(files_data.get("edit", []), "files.edit", source),
        delete=_str_list(files_data.get("delete", []), "files.delete", source),
    )
    return PlanUnit(
        slug=fq_id,
        summary=data.get("summary", ""),
        prompt=data.get("prompt", ""),
        commit_message=data.get("commit_message", ""),
        files=files,
        depends_on=_str_list(data.get("depends_on", []), "depends_on", source),
        agent=data.get("agent", ""),
        timeout_s=data.get("timeout_s", 0),
    )
