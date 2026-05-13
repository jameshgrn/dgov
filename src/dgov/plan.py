"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
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
    sop_mapping: tuple[str, ...] = ()


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


def _resolve_task_prompt(
    slug: str,
    task: DagTaskSpec,
    plan_dir: Path,
) -> str:
    """Resolve task prompt from inline text or prompt_file relative to plan_dir."""
    if task.prompt:
        return task.prompt
    if task.prompt_file:
        prompt_path = plan_dir / task.prompt_file
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Task '{slug}': prompt_file not found: {task.prompt_file} "
                f"(resolved: {prompt_path})"
            )
        return prompt_path.read_text(encoding="utf-8")
    return ""


def _dag_task_to_plan_unit(
    slug: str,
    task: DagTaskSpec,
    prompt: str,
) -> PlanUnit:
    """Convert a DagTaskSpec into a PlanUnit."""
    return PlanUnit(
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
        prompt_file=task.prompt_file,
        sop_mapping=task.sop_mapping,
        files=PlanUnitFiles(
            create=task.files.create,
            edit=task.files.edit,
            delete=task.files.delete,
            read=task.files.read,
            touch=task.files.touch,
        ),
    )


def parse_plan_file(path: str) -> PlanSpec:
    """Parse a TOML plan file into a PlanSpec."""
    dag_def = parse_dag_file(path)
    plan_path = Path(path).resolve()
    plan_dir = plan_path.parent

    units = {
        slug: _dag_task_to_plan_unit(slug, task, _resolve_task_prompt(slug, task, plan_dir))
        for slug, task in dag_def.tasks.items()
    }

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
    _raise_blocking_validation_issues(issues)

    return DagDefinition(
        name=plan.name,
        dag_file="compiled-plan",
        project_root=plan.project_root,
        session_root=plan.session_root,
        tasks={
            slug: _compile_plan_task(slug, unit, plan, project_agent)
            for slug, unit in plan.units.items()
        },
    )


def _raise_blocking_validation_issues(issues: list[PlanIssue]) -> None:
    constitutional_errors = _constitutional_errors(issues)
    if constitutional_errors:
        raise ConstitutionalViolation(constitutional_errors[0].message)

    errors = _validation_errors(issues)
    if errors:
        raise PlanValidationError(errors)


def _constitutional_errors(issues: list[PlanIssue]) -> list[PlanIssue]:
    return [
        issue
        for issue in issues
        if issue.severity == "error" and issue.message.startswith(_CONSTITUTIONAL_VIOLATION_PREFIX)
    ]


def _validation_errors(issues: list[PlanIssue]) -> list[PlanIssue]:
    return [issue for issue in issues if issue.severity == "error"]


def _compile_plan_task(
    slug: str,
    unit: PlanUnit,
    plan: PlanSpec,
    project_agent: str,
) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=unit.summary,
        prompt=unit.prompt,
        commit_message=unit.commit_message,
        agent=_resolve_task_agent(slug, unit, plan, project_agent),
        role=unit.role,
        depends_on=unit.depends_on,
        files=_compile_plan_files(unit.files),
        timeout_s=unit.timeout_s if unit.timeout_s else plan.default_timeout_s,
        iteration_budget=unit.iteration_budget,
        test_cmd=unit.test_cmd,
        sop_mapping=unit.sop_mapping,
    )


def _resolve_task_agent(
    slug: str,
    unit: PlanUnit,
    plan: PlanSpec,
    project_agent: str,
) -> str:
    agent = unit.agent or plan.default_agent or project_agent
    if agent:
        return agent
    raise PlanValidationError([
        PlanIssue(
            severity="error",
            message=f"No agent for task '{slug}'"
            " — set in task, [plan] default_agent, or project.toml",
            unit=slug,
        )
    ])


def _compile_plan_files(files: PlanUnitFiles) -> DagFileSpec:
    return DagFileSpec(
        create=files.create,
        edit=files.edit,
        delete=files.delete,
        read=files.read,
        touch=files.touch,
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
    issues.extend(_check_department_authorizations(plan, departments))
    issues.extend(_check_file_claim_conflicts(plan))
    issues.extend(_check_import_graph_conflicts(plan))
    issues.extend(_check_unclaimed_prompt_refs(plan))
    issues.extend(_check_verify_only_tasks(plan))
    issues.extend(_check_missing_task_test_cmds(plan))
    issues.extend(_check_empty_prompts(plan))
    issues.extend(_check_task_roles(plan))
    issues.extend(_check_prompt_structures(plan))
    return issues


def _check_department_authorizations(
    plan: PlanSpec,
    departments: dict[str, list[str]] | None,
) -> list[PlanIssue]:
    if not departments:
        return []
    issues: list[PlanIssue] = []
    for unit in plan.units.values():
        issues.extend(_check_department_authorization(unit, departments))
    return issues


def _check_file_claim_conflicts(plan: PlanSpec) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    slugs = list(plan.units.keys())
    for i, slug_a in enumerate(slugs):
        touches_a = _all_touches(plan.units[slug_a])
        if not touches_a:
            continue
        for slug_b in slugs[i + 1 :]:
            issues.extend(_file_conflict_issues(slug_a, touches_a, slug_b, plan.units))
    return issues


def _file_conflict_issues(
    slug_a: str,
    touches_a: set[str],
    slug_b: str,
    units: dict[str, PlanUnit],
) -> list[PlanIssue]:
    touches_b = _all_touches(units[slug_b])
    if not touches_b or not _are_independent(slug_a, slug_b, units):
        return []
    return [
        PlanIssue(
            severity="error",
            message=(
                f"File conflict: '{slug_a}' and '{slug_b}' "
                f"both touch '{path_a}' but have no dependency edge"
            ),
        )
        for path_a in touches_a
        for path_b in touches_b
        if _paths_overlap(path_a, path_b)
    ]


def _check_unclaimed_prompt_refs(plan: PlanSpec) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    for slug, unit in plan.units.items():
        issues.extend(_check_unclaimed_prompt_refs_for_unit(slug, unit))
    return issues


def _check_unclaimed_prompt_refs_for_unit(slug: str, unit: PlanUnit) -> list[PlanIssue]:
    if not unit.prompt:
        return []
    claimed = _claimed_prompt_paths(unit)
    seen: set[str] = set()
    issues: list[PlanIssue] = []
    for match in _PROMPT_PATH_RE.finditer(_prompt_reference_body(unit.prompt)):
        ref_path = _normalize_touch_path(match.group(1))
        if ref_path in seen or ref_path in claimed:
            continue
        seen.add(ref_path)
        issues.append(
            PlanIssue(
                severity="warning",
                message=(
                    f"Prompt references '{match.group(1)}' but it is not in the file claim. "
                    "Workers that touch unclaimed files get scope_violation (terminal, no retry). "
                    "Add to files.edit or files.read if the task needs this file."
                ),
                unit=slug,
            )
        )
    return issues


def _prompt_reference_body(prompt: str) -> str:
    """Scan the task body, not SOP text prepended by the compiler."""
    match = _PROMPT_PHASE_RES["Orient"].search(prompt)
    if match:
        return prompt[match.start() :]
    return prompt


def _claimed_prompt_paths(unit: PlanUnit) -> set[str]:
    return {
        _normalize_touch_path(path)
        for path in (*unit.files.create, *unit.files.edit, *unit.files.touch, *unit.files.read)
        if path.strip()
    }


def _check_verify_only_tasks(plan: PlanSpec) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    for slug, unit in plan.units.items():
        issues.extend(_check_verify_only_task(slug, unit))
    return issues


def _check_missing_task_test_cmds(plan: PlanSpec) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    for slug, unit in plan.units.items():
        issues.extend(_check_missing_task_test_cmd(slug, unit))
    return issues


def _check_empty_prompts(plan: PlanSpec) -> list[PlanIssue]:
    return [
        PlanIssue(
            severity="error",
            message=f"Task '{slug}' has an empty prompt.",
            unit=slug,
        )
        for slug, unit in plan.units.items()
        if not (unit.prompt or "").strip() and unit.role != "reviewer"
    ]


def _check_task_roles(plan: PlanSpec) -> list[PlanIssue]:
    return [
        PlanIssue(
            severity="error",
            message=(
                f"Unknown task role '{unit.role}' for '{slug}'. "
                f"Expected one of: {', '.join(sorted(_TASK_ROLES))}."
            ),
            unit=slug,
        )
        for slug, unit in plan.units.items()
        if unit.role not in _TASK_ROLES
    ]


def _check_prompt_structures(plan: PlanSpec) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
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
    issues: list[PlanIssue] = []
    for norm_path in _modifying_file_paths(unit):
        issue = _department_authorization_issue(unit, norm_path, departments)
        if issue is not None:
            issues.append(issue)
    return issues


def _modifying_file_paths(unit: PlanUnit) -> list[str]:
    return [
        normalized
        for path in (
            *unit.files.create,
            *unit.files.edit,
            *unit.files.delete,
            *unit.files.touch,
        )
        if (normalized := _normalize_touch_path(path))
    ]


def _department_authorization_issue(
    unit: PlanUnit,
    norm_path: str,
    departments: dict[str, list[str]],
) -> PlanIssue | None:
    for dept_name, patterns in departments.items():
        if not _department_owns_path(norm_path, patterns):
            continue
        if _unit_authorized_for_department(unit, dept_name):
            return None
        return PlanIssue(
            severity="error",
            message=(
                f"Constitutional violation: unit touches '{norm_path}' "
                f"owned by department '{dept_name}' but summary "
                f"does not explicitly opt-in. Add '{dept_name}' to summary."
            ),
            unit=unit.slug,
        )
    return None


def _department_owns_path(norm_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(norm_path, pattern) for pattern in patterns)


def _unit_authorized_for_department(unit: PlanUnit, dept_name: str) -> bool:
    return dept_name.lower() in (unit.summary or "").lower()


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
