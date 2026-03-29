"""Plan schema, validator, and compiler for dgov.
# selftest marker — safe to remove

This module implements the structured artifact between governor planning
and DAG execution. The governor writes a TOML plan file; this module parses,
validates, and compiles it into a DagDefinition that the existing DagKernel
can execute mechanically.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.dag_graph import _paths_overlap

if TYPE_CHECKING:
    from dgov.dag_parser import DagDefinition, DagRunSummary


@dataclass(frozen=True)
class AcceptanceCriteria:
    """What 'done' means for a plan unit."""

    tests_pass: bool = True
    lint_clean: bool = True
    custom_check: str | None = None  # shell command, exit 0 = pass


@dataclass(frozen=True)
class PlanUnitFiles:
    """Exact file scope for a plan unit."""

    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    read: tuple[str, ...] = ()  # context only, not edited


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
    agent: str | None = None  # empty = use plan default
    acceptance: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    timeout_s: int = 0  # 0 = use plan default
    escalation: tuple[str, ...] = ()
    review_agent: str | None = None  # model for reviewing this unit's output
    role: str = "worker"
    template: str | None = None
    template_vars: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanSpec:
    """A governor's execution plan — the contract between planning and execution."""

    name: str
    goal: str
    units: dict[str, PlanUnit]
    evals: tuple[PlanEval, ...] = ()
    project_root: str = "."
    session_root: str = "."
    max_concurrent: int = 0
    merge_strategy: str = "squash"
    default_agent: str = "qwen-9b"
    default_timeout_s: int = 600
    permission_mode: str = "bypassPermissions"
    max_retries: int = 1
    merge_resolve: str = "skip"
    default_review_agent: str | None = None


@dataclass(frozen=True)
class PlanIssue:
    """A validation issue found in a plan."""

    severity: str  # "error" or "warning"
    message: str
    unit: str | None = None


_SCRATCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EVAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_ALLOWED_EVAL_KINDS = {
    "regression",
    "happy_path",
    "edge",
    "invariant",
    "non_goal",
    "manual",
    "performance",
    "integration_test",
}
_VALID_ROLES = {"worker", "lt-gov"}


def _validate_scratch_name(name: str) -> str:
    """Validate a scratch plan name for safe filesystem use."""
    if not _SCRATCH_NAME_RE.match(name):
        raise ValueError(
            f"Invalid scratch plan name: {name!r}. "
            "Use 1-64 chars: lowercase letters, digits, hyphens, or underscores."
        )
    return name


def scratch_plans_dir(project_root: str, session_root: str | None = None) -> Path:
    """Return the canonical directory for ephemeral scratch plans."""
    root = Path(session_root or project_root).resolve()
    return root / ".dgov" / "plans"


def scratch_plan_path(
    name: str,
    *,
    project_root: str = ".",
    session_root: str | None = None,
) -> Path:
    """Return the canonical path for a scratch plan file."""
    scratch_name = _validate_scratch_name(name)
    return scratch_plans_dir(project_root, session_root) / f"{scratch_name}.toml"


def _scratch_plan_template(name: str) -> str:
    """Build the default scratch plan skeleton."""
    scratch_name = _validate_scratch_name(name)
    return f"""# dgov plan template — edit, then: dgov plan validate / compile / run
# Eval kinds: happy_path, regression, edge, invariant, non_goal,
#             manual, performance, integration_test

[plan]
version = 1
name = "{scratch_name}"
goal = "TODO: one-sentence goal"
default_agent = "qwen-35b"
# max_concurrent = 2
# max_retries = 2
# default_review_agent = "qwen-35b"

[[evals]]
id = "E1"
kind = "happy_path"
statement = "TODO: falsifiable success condition"
evidence = "uv run pytest tests/test_TODO.py -q"

[[evals]]
id = "E2"
kind = "regression"
statement = "TODO: what must not break"
evidence = "uv run pytest tests/ -q -m unit -k TODO"

[units.impl]
summary = "TODO: one-line summary (<=80 chars)"
prompt = \"\"\"
1. Read src/TODO.py.
2. TODO: describe the change.
3. git add src/TODO.py
4. git commit -m "TODO: commit message"
\"\"\"
commit_message = "TODO: commit message"
satisfies = ["E1", "E2"]
# depends_on = ["other-unit"]
# role = "worker"           # or "lt-gov"
# review_agent = "qwen-35b"

[units.impl.files]
edit = ["src/TODO.py"]
read = ["tests/test_TODO.py"]

[units.impl.acceptance]
tests_pass = true
lint_clean = true
# custom_check = "uv run ruff check src/TODO.py"
"""


def write_scratch_plan(
    name: str,
    *,
    project_root: str = ".",
    session_root: str | None = None,
    force: bool = False,
) -> Path:
    """Create a scratch plan skeleton under .dgov/plans/."""
    path = scratch_plan_path(name, project_root=project_root, session_root=session_root)
    if path.exists() and not force:
        raise ValueError(f"Scratch plan already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_scratch_plan_template(name))
    return path


def _normalize_file_specs(files: dict) -> PlanUnitFiles:
    """Normalize file spec lists: validate no globs, all relative, sort."""
    result = {}
    for key in ("create", "edit", "delete", "read"):
        result[key] = _normalize_relative_paths(
            files.get(key, []),
            label=f"file specs[{key}]",
        )
    return PlanUnitFiles(**result)


def _normalize_relative_paths(paths: list[str], *, label: str) -> tuple[str, ...]:
    """Normalize relative path lists used in plan metadata."""
    for path in paths:
        if "*" in path or "?" in path or "[" in path:
            raise ValueError(f"Globs not allowed in {label}: {path!r}")
        if Path(path).is_absolute():
            raise ValueError(f"Paths in {label} must be relative: {path!r}")
    return tuple(sorted(paths))


def _check_evidence_syntax(evidence: str) -> list[str]:
    """Check an eval evidence command for shell syntax issues.

    Returns a list of warning messages. Empty list = no issues found.
    Does NOT execute the command — only static/syntax checks.
    """
    warnings = []

    # 1. bash -n syntax check (parses but does not execute)
    import subprocess

    try:
        result = subprocess.run(
            ["bash", "-n", "-c", evidence],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            warnings.append(f"Shell syntax error: {stderr}")
    except (subprocess.TimeoutExpired, OSError):
        pass  # Can't check, skip

    # 2. Common anti-pattern: escaped pipe in grep -E (vim syntax, not ERE)
    # grep -E 'foo\|bar' should be grep -E 'foo|bar'
    # But plain grep (BRE) correctly uses \| for alternation — don't warn.
    if "\\|" in evidence and re.search(r"grep\s+-[^\s]*E", evidence):
        warnings.append("Evidence uses '\\|' with grep -E — did you mean '|' for ERE alternation?")

    return warnings


def _parse_eval(raw: dict) -> PlanEval:
    """Parse and validate a single eval block."""
    for req in ("id", "kind", "statement", "evidence"):
        if not raw.get(req):
            raise ValueError(f"Eval missing required field {req!r}")

    eval_id = str(raw["id"])
    if not _EVAL_ID_RE.match(eval_id):
        raise ValueError(
            f"Invalid eval id: {eval_id!r}. "
            "Use 1-32 chars: letters, digits, underscores, or hyphens."
        )

    return PlanEval(
        eval_id=eval_id,
        kind=str(raw["kind"]),
        statement=str(raw["statement"]),
        evidence=str(raw["evidence"]),
        scope=_normalize_relative_paths(list(raw.get("scope", ())), label=f"eval {eval_id} scope"),
    )


def _parse_acceptance(raw: dict) -> AcceptanceCriteria:
    """Parse optional acceptance subtable."""
    return AcceptanceCriteria(
        tests_pass=bool(raw.get("tests_pass", True)),
        lint_clean=bool(raw.get("lint_clean", True)),
        custom_check=str(raw.get("custom_check")) if raw.get("custom_check") is not None else None,
    )


def parse_plan_file(path: str) -> PlanSpec:
    """Parse a TOML plan file into a PlanSpec.

    Args:
        path: Path to the TOML plan file.

    Returns:
        A PlanSpec instance.

    Raises:
        ValueError: If required sections or fields are missing, or if
            file specs contain globs or absolute paths.
    """
    raw_bytes = Path(path).read_bytes()
    raw = tomllib.loads(raw_bytes.decode())

    plan_section = raw.get("plan")
    if not plan_section:
        raise ValueError("Missing [plan] section")
    if "version" not in plan_section:
        raise ValueError("Missing plan.version")
    if plan_section["version"] != 1:
        raise ValueError(f"Unsupported plan version: {plan_section['version']} (expected 1)")
    if "name" not in plan_section:
        raise ValueError("Missing plan.name")
    if "goal" not in plan_section:
        raise ValueError("Missing plan.goal")

    units_raw = raw.get("units")
    if not units_raw:
        raise ValueError("Missing [units] section")
    if len(units_raw) == 0:
        raise ValueError("[units] section must contain at least one unit")

    project_root = plan_section.get("project_root", ".")
    session_root = plan_section.get("session_root", ".")
    max_concurrent = plan_section.get("max_concurrent", 0)
    merge_strategy = plan_section.get("merge_strategy", "squash")
    default_agent = plan_section.get("default_agent", "qwen-9b")
    default_timeout_s = plan_section.get("default_timeout_s", 600)
    permission_mode = plan_section.get("permission_mode", "bypassPermissions")
    max_retries = plan_section.get("max_retries", 1)
    merge_resolve = plan_section.get("merge_resolve", "skip")
    default_review_agent = plan_section.get("default_review_agent")
    evals_raw = raw.get("evals", [])
    evals = tuple(_parse_eval(eval_raw) for eval_raw in evals_raw)

    units: dict[str, PlanUnit] = {}
    for slug, unit_raw in units_raw.items():
        units[slug] = _parse_unit(slug, unit_raw)

    return PlanSpec(
        name=plan_section["name"],
        goal=plan_section["goal"],
        evals=evals,
        units=units,
        project_root=project_root,
        session_root=session_root,
        max_concurrent=max_concurrent,
        merge_strategy=merge_strategy,
        default_agent=default_agent,
        default_timeout_s=default_timeout_s,
        permission_mode=permission_mode,
        max_retries=max_retries,
        merge_resolve=merge_resolve,
        default_review_agent=default_review_agent,
    )


def _parse_unit(slug: str, raw: dict) -> PlanUnit:
    """Parse and validate a single unit block."""
    for req in ("summary", "prompt", "commit_message"):
        if not raw.get(req):
            raise ValueError(f"Unit {slug!r}: missing required field {req!r}")

    files_raw = raw.get("files", {})
    files = _normalize_file_specs(files_raw)
    if not files.create and not files.edit and not files.delete:
        raise ValueError(f"Unit {slug!r}: must specify at least one file in create/edit/delete")

    acceptance_raw = raw.get("acceptance", {})
    acceptance = _parse_acceptance(acceptance_raw)
    depends_on = raw.get("depends_on")
    deps = raw.get("deps")
    if depends_on is not None and deps is not None and tuple(depends_on) != tuple(deps):
        raise ValueError(f"Unit {slug!r}: depends_on and deps disagree")
    unit_deps = depends_on if depends_on is not None else deps if deps is not None else ()

    return PlanUnit(
        slug=slug,
        summary=raw["summary"],
        prompt=raw["prompt"],
        commit_message=raw["commit_message"],
        files=files,
        satisfies=tuple(raw.get("satisfies", ())),
        depends_on=tuple(unit_deps),
        agent=str(raw.get("agent")) if raw.get("agent") is not None else None,
        acceptance=acceptance,
        timeout_s=int(raw.get("timeout_s", 0)),
        escalation=tuple(raw.get("escalation", ())),
        review_agent=str(raw.get("review_agent")) if raw.get("review_agent") is not None else None,
        role=str(raw.get("role", "worker")),
        template=str(raw.get("template")) if raw.get("template") is not None else None,
        template_vars=dict(raw.get("vars", {})),
    )


def validate_plan(plan: PlanSpec) -> list[PlanIssue]:
    """Validate a PlanSpec and return any issues found.

    Args:
        plan: The PlanSpec to validate.

    Returns:
        A list of PlanIssue objects. Empty list means the plan is valid.
        Does NOT raise exceptions.
    """
    issues: list[PlanIssue] = []
    units = plan.units
    unit_ids = set(units)
    evals_by_id = {plan_eval.eval_id: plan_eval for plan_eval in plan.evals}

    if not plan.evals:
        issues.append(
            PlanIssue(
                severity="error",
                message="Plan must define at least one [[evals]] entry before units",
            )
        )

    if len(evals_by_id) != len(plan.evals):
        issues.append(
            PlanIssue(
                severity="error",
                message="Eval ids must be unique",
            )
        )

    for plan_eval in plan.evals:
        if plan_eval.kind not in _ALLOWED_EVAL_KINDS:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=(
                        f"Eval {plan_eval.eval_id!r} has invalid kind {plan_eval.kind!r}; "
                        f"expected one of {sorted(_ALLOWED_EVAL_KINDS)}"
                    ),
                )
            )
        if not plan_eval.statement.strip():
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Eval {plan_eval.eval_id!r} statement must not be empty",
                )
            )
        if not plan_eval.evidence.strip():
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Eval {plan_eval.eval_id!r} evidence must not be empty",
                )
            )

    # Check eval evidence commands for shell syntax issues
    for plan_eval in plan.evals:
        if not plan_eval.evidence.strip():
            continue
        syntax_warnings = _check_evidence_syntax(plan_eval.evidence)
        for warning in syntax_warnings:
            issues.append(
                PlanIssue(
                    severity="warning",
                    message=f"Eval {plan_eval.eval_id!r} evidence: {warning}",
                )
            )

    # Check dependency refs exist
    for slug, unit in units.items():
        for dep in unit.depends_on:
            if dep not in unit_ids:
                issues.append(
                    PlanIssue(
                        severity="error",
                        message=f"Unit {slug!r} depends on {dep!r} which does not exist",
                        unit=slug,
                    )
                )

    # Check no cycles using DFS cycle detection
    visited: set[str] = set()
    path: set[str] = set()

    def _visit(node: str) -> None:
        if node in path:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Dependency cycle detected involving {node!r}",
                    unit=node,
                )
            )
            return
        if node in visited:
            return
        path.add(node)
        if node in units:
            for dep in units[node].depends_on:
                _visit(dep)
        path.discard(node)
        visited.add(node)

    for tid in units:
        _visit(tid)

    # Check each unit has at least one file in create/edit/delete
    for slug, unit in units.items():
        if not unit.files.create and not unit.files.edit and not unit.files.delete:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Unit {slug!r}: must specify at least one file in create/edit/delete",
                    unit=slug,
                )
            )

    # Check summary <= 80 chars (warning)
    for slug, unit in units.items():
        if len(unit.summary) > 80:
            issues.append(
                PlanIssue(
                    severity="warning",
                    message=(
                        f"Unit {slug!r}: summary exceeds 80 characters ({len(unit.summary)} chars)"
                    ),
                    unit=slug,
                )
            )

    # Check role is valid (error)
    for slug, unit in units.items():
        if unit.role not in _VALID_ROLES:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=(
                        f"Unit {slug!r}: invalid role {unit.role!r} — "
                        f"must be one of {sorted(_VALID_ROLES)}"
                    ),
                    unit=slug,
                )
            )

    # Check prompt non-empty (error)
    for slug, unit in units.items():
        if not unit.satisfies:
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Unit {slug!r} must declare at least one eval in satisfies",
                    unit=slug,
                )
            )
        for eval_id in unit.satisfies:
            if eval_id not in evals_by_id:
                issues.append(
                    PlanIssue(
                        severity="error",
                        message=f"Unit {slug!r} references unknown eval {eval_id!r}",
                        unit=slug,
                    )
                )
        if not unit.prompt.strip():
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Unit {slug!r}: prompt must not be empty",
                    unit=slug,
                )
            )

    # Check commit_message non-empty (error)
    for slug, unit in units.items():
        if not unit.commit_message.strip():
            issues.append(
                PlanIssue(
                    severity="error",
                    message=f"Unit {slug!r}: commit_message must not be empty",
                    unit=slug,
                )
            )

    # Check file conflicts for parallel units
    issues.extend(_check_file_conflicts(units))

    # Check write/read overlap for parallel units (warning only)
    issues.extend(_check_write_read_overlap(units))

    # Check agent names against registry (warning, fault-tolerant)
    try:
        from dgov.agents import load_registry

        registry_names = set(load_registry(plan.project_root or ".").keys())
    except Exception:
        registry_names = set()

    for slug, unit in units.items():
        effective_agent = unit.agent or plan.default_agent
        if not effective_agent:
            continue
        # Try to check against registry first
        found_in_registry = False
        if registry_names and effective_agent in registry_names:
            found_in_registry = True

        # If not in registry, check if it's routable (this is optional/fallback)
        if not found_in_registry:
            try:
                from dgov.router import is_routable as _is_routable

                if not _is_routable(effective_agent):
                    issues.append(
                        PlanIssue(
                            severity="warning",
                            message=(
                                f"Unit {slug!r}: agent {effective_agent!r} "
                                "not found in registry and not routable"
                            ),
                            unit=slug,
                        )
                    )
            except Exception:
                pass

    for plan_eval in plan.evals:
        if not any(plan_eval.eval_id in unit.satisfies for unit in units.values()):
            issues.append(
                PlanIssue(
                    severity="error",
                    message=(
                        f"Eval {plan_eval.eval_id!r} is not satisfied by any unit; "
                        "derive units from evals, not the reverse"
                    ),
                )
            )

    return issues


def _touches(unit: PlanUnit) -> set[str]:
    """Return the union of all file specs for overlap checking."""
    return set(unit.files.create) | set(unit.files.edit) | set(unit.files.delete)


def _are_parallel(units: dict[str, PlanUnit], a: str, b: str) -> bool:
    """Check if two units are parallel (no dependency chain between them)."""
    if a == b:
        return False

    # BFS from a to see if we can reach b via depends_on
    visited: set[str] = set()
    queue: list[str] = [a]

    while queue:
        current = queue.pop(0)
        if current == b:
            return False  # There is a dependency chain
        if current in visited:
            continue
        visited.add(current)
        if current in units:
            for dep in units[current].depends_on:
                if dep not in visited:
                    queue.append(dep)

    # Also check reverse: can b reach a?
    visited.clear()
    queue = [b]

    while queue:
        current = queue.pop(0)
        if current == a:
            return False  # There is a dependency chain (reverse)
        if current in visited:
            continue
        visited.add(current)
        if current in units:
            for dep in units[current].depends_on:
                if dep not in visited:
                    queue.append(dep)

    return True  # No dependency chain found


def _check_file_conflicts(units: dict[str, PlanUnit]) -> list[PlanIssue]:
    """Check for file conflicts between parallel units."""
    issues: list[PlanIssue] = []
    unit_ids = list(units.keys())

    for i, a_slug in enumerate(unit_ids):
        for b_slug in unit_ids[i + 1 :]:
            if not _are_parallel(units, a_slug, b_slug):
                continue  # Not parallel, skip conflict check

            a_unit = units[a_slug]
            b_unit = units[b_slug]
            a_files = _touches(a_unit)
            b_files = _touches(b_unit)

            for af in a_files:
                for bf in b_files:
                    if _paths_overlap(af, bf):
                        issues.append(
                            PlanIssue(
                                severity="error",
                                message=(
                                    f"Units {a_slug!r} and {b_slug!r} both touch "
                                    f"file {af!r} and are parallel"
                                ),
                                unit=a_slug,
                            )
                        )
                        break  # One conflict per pair is enough

    return issues


def _writes(unit: PlanUnit) -> set[str]:
    """Return the set of files a unit writes (edit + create)."""
    return set(unit.files.edit) | set(unit.files.create)


def _reads(unit: PlanUnit) -> set[str]:
    """Return the set of files a unit reads."""
    return set(unit.files.read)


def _check_write_read_overlap(units: dict[str, PlanUnit]) -> list[PlanIssue]:
    """Check for write/read overlaps between parallel units.

    When unit A edits a file that unit B reads, but B does not depend on A,
    merging A first can break main (B's code still references old API).
    This emits a warning, not an error, since the edit may not break the reader.
    """
    issues: list[PlanIssue] = []
    unit_ids = list(units.keys())

    for i, a_slug in enumerate(unit_ids):
        for b_slug in unit_ids[i + 1 :]:
            if not _are_parallel(units, a_slug, b_slug):
                continue  # Sequential units don't need this check

            a_unit = units[a_slug]
            b_unit = units[b_slug]

            # Check A writes, B reads
            a_writes = _writes(a_unit)
            b_reads = _reads(b_unit)
            overlap_ab = _get_path_overlap(a_writes, b_reads)
            if overlap_ab:
                files_str = ", ".join(sorted(overlap_ab))
                issues.append(
                    PlanIssue(
                        severity="warning",
                        message=(
                            f"Unit {b_slug!r} reads files that unit {a_slug!r} edits "
                            f"({files_str}) but does not depend on {a_slug!r} — "
                            "intermediate state may be broken"
                        ),
                        unit=b_slug,
                    )
                )

            # Check B writes, A reads
            b_writes = _writes(b_unit)
            a_reads = _reads(a_unit)
            overlap_ba = _get_path_overlap(b_writes, a_reads)
            if overlap_ba:
                files_str = ", ".join(sorted(overlap_ba))
                issues.append(
                    PlanIssue(
                        severity="warning",
                        message=(
                            f"Unit {a_slug!r} reads files that unit {b_slug!r} edits "
                            f"({files_str}) but does not depend on {b_slug!r} — "
                            "intermediate state may be broken"
                        ),
                        unit=a_slug,
                    )
                )

    return issues


def _get_path_overlap(set_a: set[str], set_b: set[str]) -> set[str]:
    """Return overlapping files between two sets, using path overlap logic."""
    overlap: set[str] = set()
    for path_a in set_a:
        for path_b in set_b:
            if _paths_overlap(path_a, path_b):
                overlap.add(path_a)
                break
    return overlap


def compile_plan(plan: PlanSpec) -> DagDefinition:
    """Compile a PlanSpec into a DagDefinition.

    Args:
        plan: The PlanSpec to compile.

    Returns:
        A DagDefinition ready for execution by the DagKernel.
    """
    from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
    from dgov.templates import load_templates, render_template

    all_templates = load_templates(plan.session_root)
    tasks: dict[str, DagTaskSpec] = {}
    evals_by_id = {plan_eval.eval_id: plan_eval for plan_eval in plan.evals}

    for slug, unit in plan.units.items():
        # Resolve defaults
        agent = unit.agent if unit.agent is not None else plan.default_agent
        timeout_s = unit.timeout_s if unit.timeout_s else plan.default_timeout_s
        review_agent = (
            unit.review_agent if unit.review_agent is not None else plan.default_review_agent
        )

        # Map PlanUnitFiles -> DagFileSpec (drop read files)
        dag_files = DagFileSpec(
            create=unit.files.create,
            edit=unit.files.edit,
            delete=unit.files.delete,
        )

        # Build modified prompt
        if unit.template:
            if unit.template not in all_templates:
                raise ValueError(f"Unit {slug!r}: Unknown template {unit.template!r}")
            tpl = all_templates[unit.template]
            tpl_vars = dict(unit.template_vars)
            if unit.role == "lt-gov":
                # Inject LT-GOV defaults if missing
                if "ltgov_slug" not in tpl_vars:
                    tpl_vars["ltgov_slug"] = slug
                if "default_agent" not in tpl_vars:
                    tpl_vars["default_agent"] = plan.default_agent
            modified_prompt = render_template(tpl, tpl_vars)
        else:
            # Build modified prompt with context and acceptance info (auto-structure)
            modified_prompt = unit.prompt

            # Add context files suffix if there are read files
            if unit.files.read:
                read_list = ", ".join(sorted(unit.files.read))
                modified_prompt += f"\n\n## Context files\nAlso read: {read_list}"

            # Add acceptance criteria suffix if different from defaults
            acceptance = unit.acceptance
            if (
                acceptance.tests_pass != AcceptanceCriteria().tests_pass
                or acceptance.lint_clean != AcceptanceCriteria().lint_clean
                or acceptance.custom_check != AcceptanceCriteria().custom_check
            ):
                modified_prompt += "\n\n## Acceptance criteria"
                modified_prompt += (
                    f"\n- Tests must pass: {'yes' if acceptance.tests_pass else 'no'}"
                )
                modified_prompt += (
                    f"\n- Lint must be clean: {'yes' if acceptance.lint_clean else 'no'}"
                )
                if acceptance.custom_check is not None:
                    modified_prompt += f"\n- Custom check: {acceptance.custom_check}"

        if unit.satisfies:
            modified_prompt += "\n\n## Evals to satisfy"
            for eval_id in unit.satisfies:
                plan_eval = evals_by_id.get(eval_id)
                if plan_eval is None:
                    continue
                modified_prompt += (
                    f"\n- [{plan_eval.eval_id}] {plan_eval.kind}: {plan_eval.statement}"
                )
                modified_prompt += f"\n  Evidence: {plan_eval.evidence}"
                if plan_eval.scope:
                    modified_prompt += f"\n  Scope: {', '.join(plan_eval.scope)}"

        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=unit.summary,
            prompt=modified_prompt,
            commit_message=unit.commit_message,
            agent=agent,
            escalation=unit.escalation,
            depends_on=unit.depends_on,
            files=dag_files,
            permission_mode=plan.permission_mode,
            timeout_s=timeout_s,
            tests_pass=unit.acceptance.tests_pass,
            lint_clean=unit.acceptance.lint_clean,
            post_merge_check=unit.acceptance.custom_check,
            review_agent=review_agent,
            role=unit.role,
            template=unit.template,
            template_vars=unit.template_vars,
        )

    return DagDefinition(
        name=plan.name,
        dag_file="",  # Not applicable for compiled plans
        project_root=plan.project_root,
        session_root=plan.session_root,
        default_max_retries=plan.max_retries,
        merge_resolve=plan.merge_resolve,
        merge_squash=(plan.merge_strategy == "squash"),
        max_concurrent=plan.max_concurrent,
        tasks=tasks,
    )


def serialize_plan(plan: PlanSpec) -> str:
    """Serialize a PlanSpec to TOML string via tomli_w.

    Handles all escaping automatically — no manual backslash wrangling.
    """
    import tomli_w

    doc: dict = {
        "plan": {
            "version": 1,
            "name": plan.name,
            "goal": plan.goal,
        },
    }
    p = doc["plan"]
    if plan.project_root != ".":
        p["project_root"] = plan.project_root
    if plan.session_root != ".":
        p["session_root"] = plan.session_root
    if plan.max_concurrent != 0:
        p["max_concurrent"] = plan.max_concurrent
    if plan.merge_strategy != "squash":
        p["merge_strategy"] = plan.merge_strategy
    if plan.default_agent != "qwen-9b":
        p["default_agent"] = plan.default_agent
    if plan.default_timeout_s != 600:
        p["default_timeout_s"] = plan.default_timeout_s
    if plan.permission_mode != "bypassPermissions":
        p["permission_mode"] = plan.permission_mode
    if plan.max_retries != 1:
        p["max_retries"] = plan.max_retries
    if plan.merge_resolve != "skip":
        p["merge_resolve"] = plan.merge_resolve
    if plan.default_review_agent is not None:
        p["default_review_agent"] = plan.default_review_agent

    if plan.evals:
        doc["evals"] = [
            {
                k: v
                for k, v in {
                    "id": e.eval_id,
                    "kind": e.kind,
                    "statement": e.statement,
                    "evidence": e.evidence,
                    "scope": list(e.scope) if e.scope else None,
                }.items()
                if v is not None
            }
            for e in plan.evals
        ]

    if plan.units:
        units: dict = {}
        for slug, unit in plan.units.items():
            u: dict = {
                "summary": unit.summary,
                "prompt": unit.prompt,
                "commit_message": unit.commit_message,
            }
            if unit.agent is not None:
                u["agent"] = unit.agent
            if unit.timeout_s:
                u["timeout_s"] = unit.timeout_s
            if unit.satisfies:
                u["satisfies"] = list(unit.satisfies)
            if unit.depends_on:
                u["depends_on"] = list(unit.depends_on)
            if unit.escalation:
                u["escalation"] = list(unit.escalation)
            if unit.review_agent is not None:
                u["review_agent"] = unit.review_agent
            if unit.role != "worker":
                u["role"] = unit.role
            if unit.template is not None:
                u["template"] = unit.template
            if unit.template_vars:
                u["vars"] = dict(unit.template_vars)

            files: dict = {}
            if unit.files.create:
                files["create"] = list(unit.files.create)
            if unit.files.edit:
                files["edit"] = list(unit.files.edit)
            if unit.files.delete:
                files["delete"] = list(unit.files.delete)
            if unit.files.read:
                files["read"] = list(unit.files.read)
            if files:
                u["files"] = files

            acc = unit.acceptance
            default_acc = AcceptanceCriteria()
            if acc != default_acc:
                acceptance: dict = {}
                if acc.tests_pass != default_acc.tests_pass:
                    acceptance["tests_pass"] = acc.tests_pass
                if acc.lint_clean != default_acc.lint_clean:
                    acceptance["lint_clean"] = acc.lint_clean
                if acc.custom_check is not None:
                    acceptance["custom_check"] = acc.custom_check
                if acceptance:
                    u["acceptance"] = acceptance

            units[slug] = u
        doc["units"] = units

    return tomli_w.dumps(doc)


def scaffold_plan(goal: str, files: list[str], name: str = "") -> str:
    """Generate a TOML plan template from goal and file list.

    Returns a valid TOML string ready for editing.
    """
    # Slugify goal if name is empty
    if not name:
        slug = goal.lower()
        slug = slug.replace(" ", "-")
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        slug = slug[:40]
    else:
        slug = name

    sorted_files = sorted(files)
    # Add commas to all but the last line for TOML array syntax
    files_edit_lines = [
        f'        "{f}",' if i < len(sorted_files) - 1 else f'        "{f}"'
        for i, f in enumerate(sorted_files)
    ]
    files_edit_str = "\n".join(files_edit_lines)

    scope_items = [f'"{f}"' for f in sorted_files]
    scope_str = "[" + ", ".join(scope_items) + "]"

    # Build output line by line to avoid quote escaping issues
    lines = [
        "# Plan boilerplate generated by `dgov plan scaffold`",
        "# Edit this file before running: fill in prompt, verify evals.",
        "",
        "[plan]",
        "version = 1",
        f'name = "{slug}"',
        f'goal = "{goal}"',
        'default_agent = "qwen-35b"',
        "default_timeout_s = 300",
        "max_retries = 2",
        "",
        "[[evals]]",
        'id = "E1"',
        'kind = "happy_path"',
        'statement = "TODO: describe the happy path that should succeed"',
        'evidence = "TODO: shell command to verify E1 (exit 0 = pass)"',
        f"scope = {scope_str}",
        "",
        "[[evals]]",
        'id = "E2"',
        'kind = "regression"',
        'statement = "TODO: describe what must not break"',
        'evidence = "TODO: shell command to verify E2 (exit 0 = pass)"',
        f"scope = {scope_str}",
        "",
        f"[units.{slug}]",
        'summary = "TODO: one-line summary of the unit of work"',
        'prompt = """',
        "1. Read " + ", ".join(f'"{f}"' for f in sorted_files) + " first.",
        "2. TODO: describe the implementation steps clearly.",
        "3. Run targeted validation for the files above.",
        "4. git add " + ", ".join(f'"{f}"' for f in sorted_files),
        '5. git commit -m "TODO: describe the completed change"',
        '"""',
        'commit_message = "TODO: describe the completed change"',
        'satisfies = ["E1", "E2"]',
        "",
        f"[units.{slug}.files]",
        "edit = [",
        files_edit_str,
        "]",
    ]

    return "\n".join(lines) + "\n"


def check_cross_plan_claims(plan: PlanSpec, session_root: str) -> list[PlanIssue]:
    """Check for file claim overlaps between this plan and active DAG runs.

    Returns warnings (not errors) for each overlapping file.
    """
    try:
        from dgov.persistence import list_active_dag_task_claims

        active_claims = list_active_dag_task_claims(session_root)
    except Exception:
        return []

    if not active_claims:
        return []

    # Collect all files claimed by this plan
    plan_files: set[str] = set()
    for unit in plan.units.values():
        plan_files.update(unit.files.create)
        plan_files.update(unit.files.edit)
        plan_files.update(unit.files.delete)

    if not plan_files:
        return []

    issues: list[PlanIssue] = []
    for claim_row in active_claims:
        run_id = claim_row.get("dag_run_id", "?")
        run_name = claim_row.get("dag_file", "unknown")
        task_slug = claim_row.get("task_slug", "?")
        run_files = set(claim_row.get("file_claims") or ())
        for pf in plan_files:
            for rf in run_files:
                if _paths_overlap(pf, rf):
                    stem = Path(run_name).stem
                    msg = (
                        f"File '{pf}' overlaps with '{rf}' in active DAG run {run_id} "
                        f"({stem}:{task_slug})"
                    )
                    issues.append(PlanIssue(severity="warning", message=msg))

    return issues


def build_adhoc_plan(
    *,
    slug: str,
    prompt: str,
    agent: str,
    project_root: str = ".",
    session_root: str = ".",
    permission_mode: str = "bypassPermissions",
    touches: tuple[str, ...] = (),
    max_retries: int = 1,
    timeout_s: int = 600,
    role: str = "worker",
    template: str | None = None,
    template_vars: dict[str, str] | None = None,
) -> PlanSpec:
    """Build a minimal single-unit PlanSpec from ad-hoc dispatch arguments.

    Creates a PlanSpec with one unit and one eval, suitable for ad-hoc
    task dispatch (e.g., pane create).

    Args:
        slug: Unique identifier for the unit.
        prompt: The worker prompt (first 100 chars become the plan goal).
        agent: The agent to dispatch (e.g., "qwen-35b").
        project_root: Project root directory (default ".").
        session_root: Session root directory (default ".").
        permission_mode: Permission mode (default "bypassPermissions").
        touches: Tuple of file paths the unit will edit (default empty).
        max_retries: Max retry attempts (default 1).
        timeout_s: Timeout in seconds (default 600).
        role: Unit role, "worker" or "lt-gov" (default "worker").
        template: Optional template name for prompt rendering.
        template_vars: Optional template variable substitutions.

    Returns:
        A PlanSpec ready for compilation or serialization.
    """
    goal = prompt[:100] if len(prompt) <= 100 else prompt[:97] + "..."

    # Build the eval that this unit satisfies
    adhoc_eval = PlanEval(
        eval_id="ADHOC_EVAL",
        kind="happy_path",
        statement="Ad-hoc task completed",
        evidence="true",
    )

    # Build file specs - only edit files (touches or empty)
    files = PlanUnitFiles(edit=touches)

    # Build the single unit
    unit = PlanUnit(
        slug=slug,
        summary=f"Ad-hoc: {slug}",
        prompt=prompt,
        commit_message=f"Apply {slug}",
        files=files,
        satisfies=("ADHOC_EVAL",),
        agent=agent,
        timeout_s=timeout_s,
        role=role,
        template=template,
        template_vars=template_vars or {},
    )

    return PlanSpec(
        name=slug,
        goal=goal,
        units={slug: unit},
        evals=(adhoc_eval,),
        project_root=project_root,
        session_root=session_root,
        permission_mode=permission_mode,
        max_retries=max_retries,
    )


def write_adhoc_plan(plan: PlanSpec, session_root: str) -> str:
    """Serialize and write an ad-hoc plan to .dgov/plans/<slug>.toml.

    Args:
        plan: The PlanSpec to serialize (typically from build_adhoc_plan).
        session_root: The session root directory.

    Returns:
        The absolute path to the written TOML file.
    """
    # Get the slug from the plan name (single-unit plan)
    slug = plan.name

    # Build the path: .dgov/plans/<slug>.toml under session_root
    plans_dir = scratch_plans_dir(session_root, session_root)
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / f"{slug}.toml"

    # Serialize and write
    toml_content = serialize_plan(plan)
    plan_path.write_text(toml_content)

    return str(plan_path.resolve())


def run_plan(
    plan_file: str,
    *,
    session_root: str | None = None,
    max_concurrent: int = 0,
    skip: set[str] | None = None,
) -> DagRunSummary:
    """Canonical entry point: parse, validate, compile, execute a plan.

    Args:
        plan_file: Path to the TOML plan file.
        session_root: Session root (defaults to plan's session_root).
        max_concurrent: Override max concurrent workers (0=use plan default).
        skip: Unit slugs to mark skipped when resuming a partial plan.

    Returns:
        A DagRunSummary from the execution.

    Raises:
        ValueError: If the plan fails parsing or validation.
    """
    import hashlib

    from dgov.dag import run_dag_via_kernel

    plan = parse_plan_file(plan_file)

    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        msg = "; ".join(i.message for i in errors)
        raise ValueError(f"Plan validation failed: {msg}")

    dag = compile_plan(plan)
    definition_hash = hashlib.sha256(Path(plan_file).read_bytes()).hexdigest()

    effective_concurrent = max_concurrent if max_concurrent > 0 else dag.max_concurrent

    return run_dag_via_kernel(
        dag,
        dag_key=str(Path(plan_file).resolve()),
        definition_hash=definition_hash,
        skip=skip,
        auto_merge=True,
        max_concurrent=effective_concurrent,
        plan_evals=[
            {
                "eval_id": plan_eval.eval_id,
                "kind": plan_eval.kind,
                "statement": plan_eval.statement,
                "evidence": plan_eval.evidence,
                "scope": list(plan_eval.scope),
            }
            for plan_eval in plan.evals
        ],
        unit_eval_links=[
            {"unit_slug": unit.slug, "eval_id": eval_id}
            for unit in plan.units.values()
            for eval_id in unit.satisfies
        ],
    )


def verify_eval_evidence(
    session_root: str,
    dag_run_id: int,
    *,
    project_root: str = ".",
    timeout_s: int = 60,
) -> list[dict]:
    """Run eval evidence commands and record pass/fail results.

    For each eval in the DAG run, executes the evidence command in a
    subprocess. Records results as typed rows in dag_eval_results.
    Returns the list of results.
    """
    import subprocess

    from dgov.persistence import (
        list_dag_evals,
        record_eval_result,
    )

    evals = list_dag_evals(session_root, dag_run_id)
    results = []

    for ev in evals:
        evidence = ev.get("evidence", "")
        if not evidence.strip():
            continue

        exit_code = None
        try:
            proc = subprocess.run(
                evidence,
                shell=True,
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=timeout_s,
            )
            passed = proc.returncode == 0
            exit_code = proc.returncode
            output = (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            passed = False
            output = f"Evidence command timed out after {timeout_s}s"
        except OSError as exc:
            passed = False
            output = f"Evidence command failed: {exc}"

        record_eval_result(session_root, dag_run_id, ev["eval_id"], passed, exit_code, output)
        result = {
            "eval_id": ev["eval_id"],
            "kind": ev["kind"],
            "statement": ev["statement"],
            "passed": passed,
            "output": output[:200],
        }
        results.append(result)

    # Note: evals_verified event is emitted by the monitor, not here.
    # This function just records results; the monitor owns the event lifecycle.

    return results
