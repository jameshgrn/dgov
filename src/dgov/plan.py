"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec, parse_dag_file
from dgov.import_graph import build_import_graph, detect_cross_task_import_conflicts
from dgov.types import ConstitutionalViolation

logger = logging.getLogger(__name__)

_TASK_ROLES = frozenset({"worker", "researcher", "reviewer"})
_CONSTITUTIONAL_VIOLATION_PREFIX = "Constitutional violation:"

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
    agent: str | None = None
    role: Literal["worker", "researcher", "reviewer"] = "worker"
    timeout_s: int | None = None
    iteration_budget: int | None = None
    test_cmd: str | None = None
    prompt_file: str | None = None


@dataclass(frozen=True)
class PlanSpec:
    """A governor's execution plan."""

    name: str
    goal: str
    units: dict[str, PlanUnit]
    project_root: str = "."
    session_root: str = "."
    default_agent: str | None = None
    default_timeout_s: int = 600
    max_retries: int = 3
    sop_set_hash: str | None = None
    source_mtime_max: str | None = None


@dataclass(frozen=True)
class PlanIssue:
    """A validation issue found in a plan."""

    severity: Literal["error", "warning"]
    message: str
    unit: str | None = None


def parse_plan_file(path: str) -> PlanSpec:
    """Parse a TOML plan file into a PlanSpec."""
    from pathlib import Path

    dag_def = parse_dag_file(path)
    plan_path = Path(path).resolve()
    plan_dir = plan_path.parent

    units = {}
    for slug, task in dag_def.tasks.items():
        # Resolve prompt_file if set
        prompt = task.prompt or ""
        prompt_file = task.prompt_file
        if prompt_file:
            prompt_path = plan_dir / prompt_file
            if not prompt_path.exists():
                raise FileNotFoundError(
                    f"Task '{slug}': prompt_file not found: {prompt_file} "
                    f"(resolved: {prompt_path})"
                )
            prompt = prompt_path.read_text(encoding="utf-8")

        units[slug] = PlanUnit(
            slug=slug,
            summary=task.summary,
            prompt=prompt,
            commit_message=task.commit_message or "",
            agent=task.agent,
            role=task.role,
            depends_on=task.depends_on,
            timeout_s=task.timeout_s,
            iteration_budget=task.iteration_budget,
            test_cmd=task.test_cmd,
            prompt_file=prompt_file,
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


def compile_plan(
    plan: PlanSpec,
    project_agent: str = "",
    departments: dict[str, list[str]] | None = None,
) -> DagDefinition:
    """Compile a PlanSpec into a DagDefinition.

    Agent resolution: task.agent → plan.default_agent → project_agent.
    Raises PlanValidationError if the plan has structural errors.
    Raises ConstitutionalViolation if file claims violate department ownership.
    """
    issues = validate_plan(plan, departments=departments)

    # Check for constitutional violations first (department ownership)
    constitutional_errors = [
        i
        for i in issues
        if i.severity == "error" and i.message.startswith(_CONSTITUTIONAL_VIOLATION_PREFIX)
    ]
    if constitutional_errors:
        raise ConstitutionalViolation(constitutional_errors[0].message)

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
            iteration_budget=unit.iteration_budget,
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


def _all_python_claims(units: dict[str, PlanUnit]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for unit in units.values():
        for raw_path in (
            *unit.files.create,
            *unit.files.edit,
            *unit.files.delete,
            *unit.files.touch,
            *unit.files.read,
        ):
            path = _normalize_touch_path(raw_path)
            if not path.endswith(".py") or path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def _to_dag_definition_for_import_analysis(plan: PlanSpec) -> DagDefinition:
    tasks: dict[str, DagTaskSpec] = {}
    for slug, unit in plan.units.items():
        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=unit.summary,
            prompt=unit.prompt,
            prompt_file=unit.prompt_file,
            commit_message=unit.commit_message,
            agent=unit.agent,
            role=unit.role,
            depends_on=unit.depends_on,
            files=DagFileSpec(
                create=unit.files.create,
                edit=unit.files.edit,
                delete=unit.files.delete,
                read=unit.files.read,
                touch=unit.files.touch,
            ),
            timeout_s=unit.timeout_s or plan.default_timeout_s,
            iteration_budget=unit.iteration_budget,
            test_cmd=unit.test_cmd,
        )
    return DagDefinition(
        name=plan.name,
        dag_file="plan-validation",
        project_root=plan.project_root,
        session_root=plan.session_root,
        tasks=tasks,
    )


def _check_import_graph_conflicts(plan: PlanSpec) -> list[PlanIssue]:
    python_files = _all_python_claims(plan.units)
    if not python_files:
        return []

    try:
        import_graph = build_import_graph(plan.project_root, python_files)
        dag = _to_dag_definition_for_import_analysis(plan)
        conflicts = detect_cross_task_import_conflicts(dag, import_graph)
    except Exception as exc:
        logger.debug("Skipping import graph conflict analysis: %s", exc)
        return []

    return [
        PlanIssue(
            severity="warning",
            message=(
                f"tasks '{conflict.task_a}' and '{conflict.task_b}' may conflict: "
                f"'{conflict.task_a}' writes {conflict.written_file} which is imported by "
                f"{conflict.importing_file} (written by '{conflict.task_b}'). "
                "Consider adding depends_on."
            ),
        )
        for conflict in conflicts
    ]


def validate_plan(
    plan: PlanSpec,
    departments: dict[str, list[str]] | None = None,
) -> list[PlanIssue]:
    """Structural validation of a plan.

    Checks:
    1. File-claim conflicts between independent tasks
    2. Prompt path references not covered by file claims
    3. Verify-only tasks with .py touch/edit claims (likely over-scoped)
    4. Prompt structure (Orient/Edit/Verify headers)
    5. Department ownership authorization (if departments config provided)
    """
    issues: list[PlanIssue] = []

    # Department ownership authorization check
    if departments:
        for _slug, unit in plan.units.items():
            issues.extend(_check_department_authorization(unit, departments))

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

    issues.extend(_check_import_graph_conflicts(plan))

    # Unclaimed prompt path reference check
    # Workers that touch unclaimed files get scope_violation (terminal, no retry).
    for slug, unit in plan.units.items():
        if not unit.prompt:
            continue
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

    # Verification specificity check: source/test write tasks should make
    # explicit when project-level auto-targeting is not sufficient.
    for slug, unit in plan.units.items():
        issues.extend(_check_missing_task_test_cmd(slug, unit))

    # Empty prompt check — catch at compile time, not at dispatch time.
    # Reviewers are exempt: they get auto-generated prompts from dependency diffs.
    for slug, unit in plan.units.items():
        if not (unit.prompt or "").strip() and unit.role != "reviewer":
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Task '{slug}' has an empty prompt.",
                    unit=slug,
                )
            )

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


def _check_missing_task_test_cmd(slug: str, unit: PlanUnit) -> list[PlanIssue]:
    """Warn when code/test write tasks rely only on project-level auto-targeting."""
    if unit.role != "worker" or unit.test_cmd:
        return []
    write_paths = [
        _normalize_touch_path(path)
        for path in (*unit.files.create, *unit.files.edit, *unit.files.touch)
        if path.strip()
    ]
    if not any(path.startswith("tests/") or "/test_" in path for path in write_paths):
        return []
    return [
        PlanIssue(
            severity="warning",
            message=(
                "Task writes test files but has no task-level test_cmd. "
                "Settlement will auto-target changed tests from project.toml; add "
                "test_cmd when this task needs a specific verification command."
            ),
            unit=slug,
        )
    ]


def _check_department_authorization(
    unit: PlanUnit,
    departments: dict[str, list[str]],
) -> list[PlanIssue]:
    """Check if a unit's file claims violate department ownership boundaries.

    A unit is authorized to touch files in a department if its summary
    contains the department name (case-insensitive substring match).
    """
    import fnmatch

    issues: list[PlanIssue] = []

    # touch is write-capable shorthand, so it must be governed like edit/create/delete.
    modifying_files = [
        *unit.files.create,
        *unit.files.edit,
        *unit.files.delete,
        *unit.files.touch,
    ]

    for file_path in modifying_files:
        norm_path = _normalize_touch_path(file_path)
        if not norm_path:
            continue

        # Check which department owns this path
        for dept_name, patterns in departments.items():
            dept_owns = False
            for pattern in patterns:
                if fnmatch.fnmatch(norm_path, pattern):
                    dept_owns = True
                    break

            if dept_owns:
                # Check if unit is authorized (summary contains department name)
                summary_lower = (unit.summary or "").lower()
                dept_lower = dept_name.lower()
                if dept_lower not in summary_lower:
                    issues.append(
                        PlanIssue(
                            severity="error",
                            message=(
                                f"Constitutional violation: unit touches '{norm_path}' "
                                f"owned by department '{dept_name}' but summary "
                                f"does not explicitly opt-in. Add '{dept_name}' to summary."
                            ),
                            unit=unit.slug,
                        )
                    )
                # Only report once per file, even if multiple patterns match
                break

    return issues


def _check_prompt_structure(slug: str, unit: PlanUnit) -> list[PlanIssue]:
    """Warn when a prompt is missing Orient/Edit/Verify section headers."""
    if not unit.prompt:
        return [
            PlanIssue(
                severity="warning",
                message="Prompt is missing section headers: Orient, Edit, Verify. "
                "Structured prompts (Orient/Edit/Verify) have higher "
                "success rates. Add headers: ## Orient, ## Edit, ## Verify.",
                unit=slug,
            )
        ]
    missing = [
        phase for phase, pattern in _PROMPT_PHASE_RES.items() if not pattern.search(unit.prompt)
    ]
    # Read-only roles don't edit or verify — only Orient matters.
    if unit.role in ("researcher", "reviewer"):
        missing = [p for p in missing if p == "Orient"]
    # test_cmd means settlement runs verification automatically.
    elif unit.test_cmd and "Verify" in missing:
        missing.remove("Verify")
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
