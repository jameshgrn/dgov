"""SOP bundler — phase 5 of the compile pipeline.

Loads markdown SOPs from `.dgov/sops/`, picks per-unit assignments via a
SopBundler protocol, and prepends selected SOP bodies to unit prompts.
See .dgov/plans/plan-system/DESIGN.md for the full compile pipeline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from dgov.plan import PlanUnit
from dgov.plan_tree import FlatPlan

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Sop:
    """A parsed markdown SOP."""

    name: str
    title: str
    body: str
    path: Path


@dataclass(frozen=True)
class BundleResult:
    """Output of the SOP bundling phase."""

    plan: FlatPlan
    sop_mapping: dict[str, tuple[str, ...]]
    sop_set_hash: str


class SopBundler(Protocol):
    """Protocol for SOP-to-unit assignment strategies."""

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        """Map each unit ID to a list of SOP names."""
        ...


class IdentityBundler:
    """Test stub — returns empty mapping for each unit."""

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        return {uid: [] for uid in units}


class LLMSopBundler:
    """Production bundler — one governor LLM call to pick SOPs per unit.

    Not wired to an LLM yet. Raises NotImplementedError until production
    LLM integration is added.
    """

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        raise NotImplementedError("LLMSopBundler requires LLM integration — not yet implemented")


def load_sops(sops_dir: Path) -> list[Sop]:
    """Load all *.md files from sops_dir, parsing YAML-like front-matter.

    Files without valid front-matter (missing ``---`` delimiters or missing
    ``name`` field) are silently skipped.
    """
    if not sops_dir.is_dir():
        return []
    sops: list[Sop] = []
    for md_path in sorted(sops_dir.glob("*.md")):
        sop = _parse_sop(md_path)
        if sop is not None:
            sops.append(sop)
    return sops


def compute_sop_set_hash(sops: list[Sop]) -> str:
    """SHA256 of sorted (filename, title) pairs — cache key for SOP set."""
    pairs = sorted((s.path.name, s.title) for s in sops)
    content = "\n".join(f"{fn}\t{title}" for fn, title in pairs)
    return hashlib.sha256(content.encode()).hexdigest()


def bundle(
    plan: FlatPlan,
    sops_dir: Path,
    bundler: SopBundler,
) -> BundleResult:
    """Run SOP bundling: load SOPs, pick per-unit, prepend to prompts.

    When no SOPs exist, returns the plan unchanged with empty mapping and
    empty hash string. Caching (reuse of prior mapping when hash matches)
    is a CLI concern — this function is stateless.
    """
    sops = load_sops(sops_dir)

    if not sops:
        return BundleResult(
            plan=plan,
            sop_mapping={uid: () for uid in plan.units},
            sop_set_hash="",
        )

    hash_val = compute_sop_set_hash(sops)
    mapping = bundler.pick(plan.units, sops)
    sop_by_name = {s.name: s for s in sops}

    rewritten: dict[str, PlanUnit] = {}
    final_mapping: dict[str, tuple[str, ...]] = {}

    for uid, unit in plan.units.items():
        picked_names = mapping.get(uid, [])
        final_mapping[uid] = tuple(picked_names)

        bodies = [sop_by_name[n].body for n in picked_names if n in sop_by_name]
        if bodies:
            rewritten[uid] = replace(unit, prompt="\n\n".join(bodies) + "\n\n" + unit.prompt)
        else:
            rewritten[uid] = unit

    return BundleResult(
        plan=replace(plan, units=rewritten),
        sop_mapping=final_mapping,
        sop_set_hash=hash_val,
    )


def _parse_sop(path: Path) -> Sop | None:
    """Parse a markdown SOP with YAML-like front-matter (name + title).

    Returns None if front-matter is absent or missing ``name``.
    """
    text = path.read_text()
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return None

    name = ""
    title = ""
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            name = stripped[len("name:") :].strip().strip("\"'")
        elif stripped.startswith("title:"):
            title = stripped[len("title:") :].strip().strip("\"'")

    if not name:
        return None

    return Sop(name=name, title=title, body=text[m.end() :].strip(), path=path)
