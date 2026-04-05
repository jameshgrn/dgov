"""Plan schema, validator, and compiler for dgov.

Pillar #1: Separation of Powers - The Plan is the contract between Governor and Worker.
Pillar #4: Determinism - Validates all inputs and dependencies before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
class AcceptanceCriteria:
    """What 'done' means for a plan unit."""

    tests_pass: bool = True
    lint_clean: bool = True
    custom_check: str = ""


@dataclass(frozen=True)
class PlanUnitFiles:
    """Exact file scope for a plan unit."""

    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    read: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanEval:
    """A falsifiable condition that defines plan success."""

    eval_id: str
    kind: str
    statement: str
    evidence: str
    scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanUnit:
    """A single unit of work in a plan."""

    slug: str
    summary: str
    prompt: str
    commit_message: str
    files: PlanUnitFiles
    satisfies: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    agent: str = ""
    acceptance: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    timeout_s: int = 0
    escalation: tuple[str, ...] = ()
    review_agent: str = ""
    role: str = "worker"


@dataclass(frozen=True)
class PlanSpec:
    """A governor's execution plan."""

    name: str
    goal: str
    units: dict[str, PlanUnit]
    evals: tuple[PlanEval, ...] = ()
    project_root: str = "."
    session_root: str = "."
    max_concurrent: int = 0
    merge_strategy: str = "ff-only"
    default_agent: str = "qwen-35b"
    default_timeout_s: int = 600
    permission_mode: str = "bypassPermissions"
    max_retries: int = 1
    merge_resolve: str = "skip"
    default_review_agent: str = ""


@dataclass(frozen=True)
class PlanIssue:
    """A validation issue found in a plan."""

    severity: str  # "error" or "warning"
    message: str
    unit: Optional[str] = None


def parse_plan_file(path: str) -> PlanSpec:
    """Parse a TOML plan file into a PlanSpec."""
    # We use our new Pydantic-powered dag_parser to do the heavy lifting
    dag_def = parse_dag_file(path)

    # Map back to PlanSpec for legacy CLI compatibility if needed
    # (Though we should eventually just use DagDefinition everywhere)
    units = {}
    for slug, task in dag_def.tasks.items():
        units[slug] = PlanUnit(
            slug=slug,
            summary=task.summary,
            prompt=task.prompt,
            commit_message=task.commit_message,
            agent=task.agent,
            depends_on=task.depends_on,
            escalation=task.escalation,
            timeout_s=task.timeout_s,
            review_agent=task.review_agent or "",
            role=task.role,
            files=PlanUnitFiles(
                create=task.files.create, edit=task.files.edit, delete=task.files.delete
            ),
            acceptance=AcceptanceCriteria(
                tests_pass=task.tests_pass,
                lint_clean=task.lint_clean,
                custom_check=task.post_merge_check or "",
            ),
        )

    evals = tuple(
        PlanEval(eval_id=ev.id, kind=ev.kind, statement=ev.statement, evidence=ev.evidence)
        for ev in dag_def.evals
    )

    return PlanSpec(
        name=dag_def.name,
        goal="Automated Goal",
        project_root=dag_def.project_root,
        session_root=dag_def.session_root,
        max_concurrent=dag_def.max_concurrent,
        units=units,
        evals=evals,
        max_retries=dag_def.default_max_retries,
        merge_resolve=dag_def.merge_resolve,
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
    evals_by_id = {plan_eval.eval_id: plan_eval for plan_eval in plan.evals}

    for slug, unit in plan.units.items():
        agent = unit.agent if unit.agent else plan.default_agent
        timeout_s = unit.timeout_s if unit.timeout_s else plan.default_timeout_s
        review_agent = unit.review_agent if unit.review_agent else plan.default_review_agent

        dag_files = DagFileSpec(
            create=unit.files.create,
            edit=unit.files.edit,
            delete=unit.files.delete,
        )

        final_prompt = unit.prompt
        if unit.satisfies:
            final_prompt += "\n\n## Evals to satisfy\n"
            for eid in unit.satisfies:
                if eid in evals_by_id:
                    ev = evals_by_id[eid]
                    final_prompt += (
                        f"- [{eid}] {ev.kind}: {ev.statement}\n  Evidence: {ev.evidence}\n"
                    )

        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=unit.summary,
            prompt=final_prompt,
            commit_message=unit.commit_message,
            agent=agent,
            escalation=unit.escalation,
            depends_on=unit.depends_on,
            files=dag_files,
            timeout_s=timeout_s,
            review_agent=review_agent,
            role=unit.role,
        )

    return DagDefinition(
        name=plan.name,
        dag_file="compiled-plan",
        project_root=plan.project_root,
        session_root=plan.session_root,
        max_concurrent=plan.max_concurrent,
        tasks=tasks,
        merge_resolve=plan.merge_resolve,
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
