"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec, parse_dag_file

# Matches test-file paths embedded in prompt text, e.g. "tests/test_foo.py"
_TEST_PATH_RE = re.compile(r"\b(tests?/[\w/.+-]+\.py)\b")


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
    touch: tuple[str, ...] = ()


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
    default_agent: str = ""
    default_timeout_s: int = 600
    max_retries: int = 1
    sop_set_hash: str = ""
    source_mtime_max: str = ""


@dataclass(frozen=True)
class PlanIssue:
    """A validation issue found in a plan."""

    severity: str  # "error" or "warning"
    message: str
    unit: str | None = None


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
                create=task.files.create,
                edit=task.files.edit,
                delete=task.files.delete,
                touch=task.files.touch,
            ),
        )

    return PlanSpec(
        name=dag_def.name,
        goal="Automated Goal",
        project_root=dag_def.project_root,
        session_root=dag_def.session_root,
        units=units,
        default_agent=dag_def.default_agent,
        max_retries=dag_def.default_max_retries,
        sop_set_hash=dag_def.sop_set_hash,
        source_mtime_max=dag_def.source_mtime_max,
    )


class PlanValidationError(ValueError):
    """Raised when a plan has structural errors that prevent execution."""

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        msgs = [f"[{i.severity}] {i.message}" for i in issues]
        super().__init__("Plan validation failed:\n" + "\n".join(msgs))


def compile_plan(plan: PlanSpec, project_agent: str = "") -> DagDefinition:
    """Compile a PlanSpec into a DagDefinition.

    Agent resolution: task.agent → plan.default_agent → project_agent.
    Raises PlanValidationError if the plan has structural errors.
    """
    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise PlanValidationError(errors)

    tasks: dict[str, DagTaskSpec] = {}

    for slug, unit in plan.units.items():
        agent = unit.agent or plan.default_agent or project_agent
        if not agent:
            raise PlanValidationError([
                PlanIssue(
                    severity="error",
                    message=f"No agent for task '{slug}'"
                    " — set in task, [plan] default_agent, or project.toml",
                    unit=slug,
                )
            ])
        timeout_s = unit.timeout_s if unit.timeout_s else plan.default_timeout_s

        dag_files = DagFileSpec(
            create=unit.files.create,
            edit=unit.files.edit,
            delete=unit.files.delete,
            touch=unit.files.touch,
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
    """All file paths a unit claims to touch (create + edit + delete + touch)."""
    return {
        _normalize_touch_path(p)
        for p in (*unit.files.create, *unit.files.edit, *unit.files.delete, *unit.files.touch)
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
    2. Test file references in prompts that are not in the file claim
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

    # Unclaimed test file reference check
    # Workers that touch unclaimed files get scope_violation (terminal, no retry).
    for slug, unit in plan.units.items():
        claimed = {
            _normalize_touch_path(p)
            for p in (*unit.files.create, *unit.files.edit, *unit.files.touch)
            if p.strip()
        }
        seen: set[str] = set()
        for m in _TEST_PATH_RE.finditer(unit.prompt):
            test_path = _normalize_touch_path(m.group(1))
            if test_path in seen or test_path in claimed:
                continue
            seen.add(test_path)
            issues.append(
                PlanIssue(
                    severity="warning",
                    message=(
                        f"Prompt references '{m.group(1)}' but it is not in the file claim. "
                        "Workers that touch unclaimed files get scope_violation (terminal, no retry). "
                        "Add to files.edit if the task may modify tests."
                    ),
                    unit=slug,
                )
            )

    return issues
