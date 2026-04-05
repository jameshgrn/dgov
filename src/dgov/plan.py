"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec, parse_dag_file


def _normalize_touch_path(path: str) -> str:
    """Normalize a file path for comparison."""
    return path.strip().lstrip("./").rstrip("/")


def _paths_overlap(path: str, touch: str) -> bool:
    """Check if two paths overlap (identical or one is a parent of other)."""
    norm_path = _normalize_touch_path(path)
    norm_touch = _normalize_touch_path(touch)
    if not norm_path or not norm_touch:
        return False
    return (
        norm_path == norm_touch
        or norm_path.startswith(norm_touch + "/")
        or norm_touch.startswith(norm_path + "/")
    )


@dataclass(frozen=True)
class PlanUnitFiles:
    """Exact file scope for a plan unit."""

    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    read: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanUnit:
    """A single unit of work in a plan."""

    slug: str
    summary: str
    prompt: str
    commit_message: str
    files: PlanUnitFiles
    depends_on: tuple[str, ...] = ()
    agent: str = ""
    timeout_s: int = 0


@dataclass(frozen=True)
class PlanSpec:
    """A governor's execution plan."""

    name: str
    goal: str
    units: dict[str, PlanUnit]
    project_root: str = "."
    session_root: str = "."
    default_agent: str = "qwen-35b"
    default_timeout_s: int = 600
    max_retries: int = 1


@dataclass(frozen=True)
class PlanIssue:
    """A validation issue found in a plan."""

    severity: str  # "error" or "warning"
    message: str
    unit: Optional[str] = None


def parse_plan_file(path: str) -> PlanSpec:
    """Parse a TOML plan file into a PlanSpec."""
    dag_def = parse_dag_file(path)

    units = {}
    for slug, task in dag_def.tasks.items():
        units[slug] = PlanUnit(
            slug=slug,
            summary=task.summary,
            prompt=task.prompt,
            commit_message=task.commit_message,
            agent=task.agent,
            depends_on=task.depends_on,
            timeout_s=task.timeout_s,
            files=PlanUnitFiles(
                create=task.files.create, edit=task.files.edit, delete=task.files.delete
            ),
        )

    return PlanSpec(
        name=dag_def.name,
        goal="Automated Goal",
        project_root=dag_def.project_root,
        session_root=dag_def.session_root,
        units=units,
        max_retries=dag_def.default_max_retries,
    )


class PlanValidationError(ValueError):
    """Raised when a plan has structural errors that prevent execution."""

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        msgs = [f"[{i.severity}] {i.message}" for i in issues]
        super().__init__("Plan validation failed:\n" + "\n".join(msgs))


def compile_plan(plan: PlanSpec) -> DagDefinition:
    """Compile a PlanSpec into a DagDefinition.

    Raises PlanValidationError if the plan has structural errors.
    """
    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise PlanValidationError(errors)

    tasks: dict[str, DagTaskSpec] = {}

    for slug, unit in plan.units.items():
        agent = unit.agent if unit.agent else plan.default_agent
        timeout_s = unit.timeout_s if unit.timeout_s else plan.default_timeout_s

        dag_files = DagFileSpec(
            create=unit.files.create,
            edit=unit.files.edit,
            delete=unit.files.delete,
        )

        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=unit.summary,
            prompt=unit.prompt,
            commit_message=unit.commit_message,
            agent=agent,
            depends_on=unit.depends_on,
            files=dag_files,
            timeout_s=timeout_s,
        )

    return DagDefinition(
        name=plan.name,
        dag_file="compiled-plan",
        project_root=plan.project_root,
        session_root=plan.session_root,
        tasks=tasks,
    )


def _all_touches(unit: PlanUnit) -> set[str]:
    """All file paths a unit claims to touch (create + edit + delete)."""
    return {
        _normalize_touch_path(p)
        for p in (*unit.files.create, *unit.files.edit, *unit.files.delete)
        if p.strip()
    }


def _are_independent(a: str, b: str, units: dict[str, PlanUnit]) -> bool:
    """True if neither unit depends (directly or transitively) on the other."""

    # Build reachability from depends_on
    def _reachable(start: str) -> set[str]:
        visited: set[str] = set()
        stack = list(units[start].depends_on) if start in units else []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            if node in units:
                stack.extend(units[node].depends_on)
        return visited

    return b not in _reachable(a) and a not in _reachable(b)


def validate_plan(plan: PlanSpec) -> list[PlanIssue]:
    """Structural validation of a plan.

    Checks:
    1. File-claim conflicts between independent tasks
    """
    issues: list[PlanIssue] = []

    # File-claim conflict detection between independent tasks
    slugs = list(plan.units.keys())
    for i, slug_a in enumerate(slugs):
        touches_a = _all_touches(plan.units[slug_a])
        if not touches_a:
            continue
        for slug_b in slugs[i + 1 :]:
            touches_b = _all_touches(plan.units[slug_b])
            if not touches_b:
                continue
            if not _are_independent(slug_a, slug_b, plan.units):
                continue
            # Check for overlapping paths
            for pa in touches_a:
                for pb in touches_b:
                    if _paths_overlap(pa, pb):
                        issues.append(
                            PlanIssue(
                                severity="error",
                                message=(
                                    f"File conflict: '{slug_a}' and '{slug_b}' "
                                    f"both touch '{pa}' but have no dependency edge"
                                ),
                            )
                        )

    return issues
