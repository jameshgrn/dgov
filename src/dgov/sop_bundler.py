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
_SECTION_ORDER = (
    ("When", "when"),
    ("Do", "do"),
    ("Do Not", "do_not"),
    ("Verify", "verify"),
    ("Escalate", "escalate"),
)
_VALID_PRIORITIES = frozenset({"must", "should"})


@dataclass(frozen=True)
class Sop:
    """A parsed markdown SOP."""

    name: str
    title: str
    summary: str
    applies_to: tuple[str, ...]
    priority: str
    when: tuple[str, ...]
    do: tuple[str, ...]
    do_not: tuple[str, ...]
    verify: tuple[str, ...]
    escalate: tuple[str, ...]
    path: Path

    def render_prompt_block(self) -> str:
        """Render this SOP into a canonical worker-facing prompt block."""
        lines = [
            f"[SOP: {self.title}]",
            f"Summary: {self.summary}",
            f"Applies To: {', '.join(self.applies_to)}",
            f"Priority: {self.priority.upper()}",
            "",
        ]
        for display, attr in _SECTION_ORDER:
            lines.append(f"{display}:")
            lines.extend(f"- {item}" for item in getattr(self, attr))
            lines.append("")
        return "\n".join(lines).strip()


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


def _normalize(word: str) -> str:
    """Strip trailing 's' for basic plural normalization."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


class TagBasedSopBundler:
    """Deterministic bundler — assigns SOPs by tag intersection.

    For each unit, extracts keywords from the summary, file extensions,
    and role, then matches against each SOP's applies_to tags. Zero
    latency, zero external dependencies.
    """

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        return {uid: self._match(unit, sops) for uid, unit in units.items()}

    @staticmethod
    def _match(unit: PlanUnit, sops: list[Sop]) -> list[str]:
        keywords = TagBasedSopBundler._extract_keywords(unit)
        normalized_kw = frozenset(_normalize(k) for k in keywords)
        return [
            s.name for s in sops if frozenset(_normalize(t) for t in s.applies_to) & normalized_kw
        ]

    @staticmethod
    def _extract_keywords(unit: PlanUnit) -> frozenset[str]:
        """Extract matching keywords from structured unit metadata.

        Only uses high-signal sources to avoid false positives from common
        English words in prompts. Sources: file extensions, role, and
        summary words (short, curated by the plan author).
        """
        tokens: set[str] = set()
        # Summary only — prompt text is too noisy (common words match SOP tags)
        if unit.summary:
            tokens.update(word.lower().strip(".,;:()\"'`") for word in unit.summary.split())
        # File extensions → language tags
        for path in (
            *(unit.files.create if unit.files else ()),
            *(unit.files.edit if unit.files else ()),
            *(unit.files.touch if unit.files else ()),
        ):
            if path.endswith(".py"):
                tokens.add("python")
            elif path.endswith((".js", ".jsx", ".ts", ".tsx")):
                tokens.update(("javascript", "typescript"))
        # Role-based tags
        if unit.role in ("reviewer", "researcher"):
            tokens.update(("review", "reviewer"))
        return frozenset(tokens)


def load_sops(sops_dir: Path) -> list[Sop]:
    """Load all *.md files from sops_dir, enforcing the standard SOP format."""
    if not sops_dir.is_dir():
        return []
    sops: list[Sop] = []
    errors: list[str] = []
    for md_path in sorted(sops_dir.glob("*.md")):
        try:
            sops.append(_parse_sop(md_path))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        joined = "\n".join(f"- {err}" for err in errors)
        raise ValueError(f"Invalid SOP files in {sops_dir}:\n{joined}")
    return sops


def compute_sop_set_hash(sops: list[Sop]) -> str:
    """SHA256 of sorted selection metadata — cache key for SOP assignment."""
    pairs = sorted(
        (
            s.path.name,
            s.name,
            s.title,
            s.summary,
            ",".join(s.applies_to),
            s.priority,
        )
        for s in sops
    )
    content = "\n".join(
        f"{filename}\t{name}\t{title}\t{summary}\t{applies_to}\t{priority}"
        for filename, name, title, summary, applies_to, priority in pairs
    )
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

        bodies = [sop_by_name[n].render_prompt_block() for n in picked_names if n in sop_by_name]
        if bodies:
            prompt_suffix = unit.prompt or ""
            rewritten[uid] = replace(unit, prompt="\n\n".join(bodies) + "\n\n" + prompt_suffix)
        else:
            rewritten[uid] = unit

    return BundleResult(
        plan=replace(plan, units=rewritten),
        sop_mapping=final_mapping,
        sop_set_hash=hash_val,
    )


def _parse_sop(path: Path) -> Sop:
    """Parse a markdown SOP with required metadata and canonical sections."""
    text = path.read_text()
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing front matter")

    front_matter = _parse_front_matter(path, m.group(1))
    name = _require_string(front_matter, path, "name")
    title = _require_string(front_matter, path, "title")
    summary = _require_string(front_matter, path, "summary")
    applies_to = _require_string_list(front_matter, path, "applies_to")
    priority = _require_string(front_matter, path, "priority").lower()
    if priority not in _VALID_PRIORITIES:
        valid = ", ".join(sorted(_VALID_PRIORITIES))
        raise ValueError(f"{path.name}: priority must be one of {valid}, got {priority!r}")

    sections = _parse_sections(path, text[m.end() :].strip())
    return Sop(
        name=name,
        title=title,
        summary=summary,
        applies_to=applies_to,
        priority=priority,
        when=sections["when"],
        do=sections["do"],
        do_not=sections["do_not"],
        verify=sections["verify"],
        escalate=sections["escalate"],
        path=path,
    )


def _parse_front_matter(path: Path, text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise ValueError(f"{path.name}: invalid front-matter line: {line!r}")
        key, value = stripped.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def _require_string(data: dict[str, str], path: Path, key: str) -> str:
    value = data.get(key, "").strip().strip("\"'")
    if not value:
        raise ValueError(f"{path.name}: missing required front-matter field {key!r}")
    return value


def _require_string_list(data: dict[str, str], path: Path, key: str) -> tuple[str, ...]:
    raw = data.get(key, "").strip()
    if not raw:
        raise ValueError(f"{path.name}: missing required front-matter field {key!r}")
    if not raw.startswith("[") or not raw.endswith("]"):
        raise ValueError(f"{path.name}: {key!r} must be a bracketed list")
    inner = raw[1:-1].strip()
    if not inner:
        raise ValueError(f"{path.name}: {key!r} must not be empty")
    items = tuple(item.strip().strip("\"'") for item in inner.split(",") if item.strip())
    if not items:
        raise ValueError(f"{path.name}: {key!r} must not be empty")
    return items


def _parse_sections(path: Path, body: str) -> dict[str, tuple[str, ...]]:
    if not body:
        raise ValueError(f"{path.name}: SOP body is empty")

    parsed: dict[str, list[str]] = {key: [] for _, key in _SECTION_ORDER}
    current: str | None = None
    heading_map: dict[str, str] = {display.lower(): key for display, key in _SECTION_ORDER}

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            current = heading_map.get(heading)
            if current is None:
                raise ValueError(f"{path.name}: unknown SOP section {stripped!r}")
            continue
        if current is None:
            raise ValueError(f"{path.name}: content must appear under a supported ## section")
        if stripped.startswith("- ") or stripped.startswith("* "):
            parsed[current].append(stripped[2:].strip())
            continue
        if line[:1].isspace() and parsed[current]:
            parsed[current][-1] = f"{parsed[current][-1]} {stripped}"
            continue
        raise ValueError(f"{path.name}: section content must be bullet lists; got {line!r}")

    missing = [display for display, key in _SECTION_ORDER if not parsed[key]]
    if missing:
        raise ValueError(f"{path.name}: missing required sections: {', '.join(missing)}")
    return {key: tuple(items) for key, items in parsed.items()}
