"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec, parse_dag_file

_TASK_ROLES = frozenset({"worker", "researcher"})

# Matches file paths embedded in prompt text: any word/path ending in a known
# extension (.py, .toml, .json, .yaml, .yml, .md, .txt, .cfg, .ini, .sh)
# that contains at least one directory separator.
_PROMPT_PATH_RE = re.compile(
    r"(?<!\w)(\.?[\w.+-]+/[\w/.+-]+\.(?:py|toml|json|yaml|yml|md|txt|cfg|ini|sh))\b"
)

# Verbs that signal a cross-cutting task — these should claim the full call chain.
_CROSSCUT_VERBS_RE = re.compile(
    r"\b(fix|stabilize|stabilise|clean\s*up|refactor|migrate)\b", re.IGNORECASE
)

# Orient/Edit/Verify section headers in prompt text.
# Matches isolated headings like "Orient:", "## Orient", "**Orient:**", etc.
_PROMPT_PHASE_RES = {
    "Orient": re.compile(
        r"^\s*(?:#{1,6}\s+)?(?:\*\*)?orient(?::)?(?:\*\*)?\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "Edit": re.compile(
        r"^\s*(?:#{1,6}\s+)?(?:\*\*)?edit(?::)?(?:\*\*)?\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "Verify": re.compile(
        r"^\s*(?:#{1,6}\s+)?(?:\*\*)?verify(?::)?(?:\*\*)?\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
}


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
    role: Literal["worker", "researcher"] = "worker"
    timeout_s: int = 0
    test_cmd: str = ""


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
    max_retries: int = 3
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
            role=task.role,
            depends_on=task.depends_on,
            timeout_s=task.timeout_s,
            test_cmd=task.test_cmd,
            files=PlanUnitFiles(
                create=task.files.create,
                edit=task.files.edit,
                delete=task.files.delete,
                read=task.files.read,
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
            read=unit.files.read,
            touch=unit.files.touch,
        )

        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=unit.summary,
            prompt=unit.prompt,
            commit_message=unit.commit_message,
            agent=agent,
            role=unit.role,
            depends_on=unit.depends_on,
            files=dag_files,
            timeout_s=timeout_s,
            test_cmd=unit.test_cmd,
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
    2. Prompt path references not covered by file claims
    3. Verify-only tasks with .py touch/edit claims (likely over-scoped)
    4. Prompt structure (Orient/Edit/Verify headers)
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

    # Unclaimed prompt path reference check
    # Workers that touch unclaimed files get scope_violation (terminal, no retry).
    for slug, unit in plan.units.items():
        claimed = {
            _normalize_touch_path(p)
            for p in (*unit.files.create, *unit.files.edit, *unit.files.touch, *unit.files.read)
            if p.strip()
        }
        seen: set[str] = set()
        for m in _PROMPT_PATH_RE.finditer(unit.prompt):
            ref_path = _normalize_touch_path(m.group(1))
            if ref_path in seen or ref_path in claimed:
                continue
            seen.add(ref_path)
            issues.append(
                PlanIssue(
                    severity="warning",
                    message=(
                        f"Prompt references '{m.group(1)}' but it is not in the file claim. "
                        "Workers that touch unclaimed files get scope_violation (terminal, no retry). "
                        "Add to files.edit or files.read if the task needs this file."
                    ),
                    unit=slug,
                )
            )

    # Verify-only task check: tasks whose only create targets are non-.py files
    # should not have .py files in touch/edit (this tempts the worker to modify code).
    for slug, unit in plan.units.items():
        issues.extend(_check_verify_only_task(slug, unit))

    # Task role check
    for slug, unit in plan.units.items():
        if unit.role not in _TASK_ROLES:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=(
                        f"Unknown task role '{unit.role}' for '{slug}'. "
                        f"Expected one of: {', '.join(sorted(_TASK_ROLES))}."
                    ),
                    unit=slug,
                )
            )

    # Prompt structure check: Orient/Edit/Verify headers.
    for slug, unit in plan.units.items():
        issues.extend(_check_prompt_structure(slug, unit))

    return issues


def _check_verify_only_task(slug: str, unit: PlanUnit) -> list[PlanIssue]:
    """Warn when a verify/capture task has .py touch/edit claims.

    A task that only creates non-code files (e.g. .json, .txt, .md) but also
    claims .py files via touch/edit is likely over-scoped — the worker will
    be tempted to modify code files, leading to scope violations on unrelated
    edits.
    """
    if not unit.files.create:
        return []
    # Only applies when ALL create targets are non-.py
    if any(f.endswith(".py") for f in unit.files.create):
        return []
    py_touches = [f for f in (*unit.files.edit, *unit.files.touch) if f.strip().endswith(".py")]
    if not py_touches:
        return []
    return [
        PlanIssue(
            severity="warning",
            message=(
                f"Task only creates non-code files ({', '.join(unit.files.create)}) "
                f"but also claims .py files via edit/touch: {py_touches}. "
                "This tempts the worker to modify code, risking scope violations. "
                "Remove the .py claims or split into separate tasks."
            ),
            unit=slug,
        )
    ]


def _check_prompt_structure(slug: str, unit: PlanUnit) -> list[PlanIssue]:
    """Warn when a prompt is missing Orient/Edit/Verify section headers."""
    missing = [
        phase for phase, pattern in _PROMPT_PHASE_RES.items() if not pattern.search(unit.prompt)
    ]
    if not missing:
        return []
    return [
        PlanIssue(
            severity="warning",
            message=(
                f"Prompt is missing section headers: {', '.join(missing)}. "
                "Structured prompts (Orient/Edit/Verify) have higher "
                "first-attempt success rates."
            ),
            unit=slug,
        )
    ]
