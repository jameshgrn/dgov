"""SOP bundler — phase 5 of the compile pipeline.

Loads markdown SOPs from `.dgov/sops/`, picks per-unit assignments via a
SopBundler protocol, and prepends selected SOP bodies to unit prompts.
See .dgov/plans/plan-system/DESIGN.md for the full compile pipeline.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from openai import OpenAI
from pydantic import BaseModel

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


class SopMappingResponse(BaseModel):
    """Structured response for SOP mapping."""

    mapping: dict[str, list[str]]


class LLMSopBundler:
    """Production bundler — one governor LLM call to pick SOPs per unit.

    Uses an OpenAI-compatible client (default: Fireworks) to map units to SOPs.
    Requires FIREWORKS_API_KEY environment variable.
    """

    def __init__(
        self,
        model: str = "accounts/fireworks/routers/kimi-k2p5-turbo",
        base_url: str = "https://api.fireworks.ai/inference/v1",
    ) -> None:
        self.model = model
        self.base_url = base_url

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        api_key = os.environ.get("FIREWORKS_API_KEY")
        if not api_key:
            raise ValueError("FIREWORKS_API_KEY missing — required for LLMSopBundler")

        client = OpenAI(base_url=self.base_url, api_key=api_key)

        sop_list = "\n".join(f"- {s.name}: {s.title}" for s in sops)
        unit_list = "\n".join(f"- {uid}: {u.summary}" for uid, u in units.items())

        prompt = f"""You are the dgov governor. Your task is to assign relevant Standard Operating
Procedures (SOPs) to each unit of work in a plan.

AVAILABLE SOPS:
{sop_list}

PLAN UNITS:
{unit_list}

For each unit, identify which SOPs are relevant to its task based on the SOP title and unit
summary. A unit can have zero, one, or multiple SOPs assigned.

Return a JSON object with a "mapping" key where each key is a unit ID and the value is a list of
SOP names.
Example:
{{
  "mapping": {{
    "unit/id.one": ["sop-a", "sop-b"],
    "unit/id.two": []
  }}
}}
"""

        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise task-to-SOP assignment engine.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            if not content:
                return {uid: [] for uid in units}

            data = SopMappingResponse.model_validate_json(content)
            return data.mapping
        except Exception as e:
            # Pillar #6: Fail fast. If the governor can't pick SOPs, compile fails.
            raise RuntimeError(f"LLMSopBundler failed: {e!s}") from e


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
    cached_mapping: dict[str, tuple[str, ...]] | None = None,
    cached_hash: str | None = None,
) -> BundleResult:
    """Run SOP bundling: load SOPs, pick per-unit, prepend to prompts.

    If cached_hash matches the current SOP set hash, cached_mapping is reused
    to skip the bundler.pick() call (usually an expensive LLM call).

    When no SOPs exist, returns the plan unchanged with empty mapping and
    empty hash string.
    """
    sops = load_sops(sops_dir)

    if not sops:
        return BundleResult(
            plan=plan,
            sop_mapping={uid: () for uid in plan.units},
            sop_set_hash="",
        )

    hash_val = compute_sop_set_hash(sops)

    # Cache hit: reuse mapping if hash matches and mapping covers all current units
    if (
        cached_hash == hash_val
        and cached_mapping is not None
        and all(uid in cached_mapping for uid in plan.units)
    ):
        mapping = {uid: list(names) for uid, names in cached_mapping.items()}
    else:
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
