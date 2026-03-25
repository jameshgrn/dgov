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
    from dgov.dag_parser import DagDefinition


@dataclass(frozen=True)
class AcceptanceCriteria:
    """What 'done' means for a plan unit."""

    tests_pass: bool = True
    lint_clean: bool = True
    custom_check: str = ""  # shell command, exit 0 = pass


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
    agent: str = ""  # empty = use plan default
    acceptance: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    timeout_s: int = 0  # 0 = use plan default
    escalation: tuple[str, ...] = ()
    review_agent: str = ""  # model for reviewing this unit's output
    role: str = "worker"
    template: str = ""
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
    default_review_agent: str = ""


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
    return f"""# Scratch plans live under .dgov/plans/ and are safe to delete when finished.
# The plan is the contract. Keep file claims exact and dependencies minimal.
# Optional plan-level routing stays here if needed:
# default_agent = "qwen-9b"

[plan]
version = 1
name = "{scratch_name}"
goal = "Replace with the concrete goal before running."

[[evals]]
id = "E1"
kind = "regression"
statement = "The scratch plan is created under .dgov/plans/ and not in the repo root."
evidence = "uv run dgov plan scratch {scratch_name}"
scope = ["src/dgov/plan.py", "src/dgov/cli/plan_cmd.py"]

[units.first_change]
summary = "Describe one concrete unit of work"
prompt = \"\"\"
1. Read the target files first.
2. Make the requested change.
3. Run targeted validation for the claimed files.
4. git add the changed files.
5. git commit -m "Describe the completed change"
\"\"\"
commit_message = "Describe the completed change"
satisfies = ["E1"]

[units.first_change.files]
edit = ["src/path/to/file.py"]
read = ["tests/test_path_to_file.py"]
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

    # 2. Common anti-pattern: escaped pipe in grep -qE (vim syntax, not ERE)
    # grep -qE 'foo\|bar' should be grep -qE 'foo|bar'
    if "\\|" in evidence and "grep" in evidence:
        warnings.append("Evidence uses '\\|' with grep — did you mean '|' for ERE alternation?")

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
        custom_check=str(raw.get("custom_check", "")),
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
    default_review_agent = plan_section.get("default_review_agent", "")
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

    return PlanUnit(
        slug=slug,
        summary=raw["summary"],
        prompt=raw["prompt"],
        commit_message=raw["commit_message"],
        files=files,
        satisfies=tuple(raw.get("satisfies", ())),
        depends_on=tuple(raw.get("depends_on", ())),
        agent=str(raw.get("agent", "")),
        acceptance=acceptance,
        timeout_s=int(raw.get("timeout_s", 0)),
        escalation=tuple(raw.get("escalation", ())),
        review_agent=str(raw.get("review_agent", "")),
        role=str(raw.get("role", "worker")),
        template=str(raw.get("template", "")),
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
        agent = unit.agent if unit.agent else plan.default_agent
        timeout_s = unit.timeout_s if unit.timeout_s else plan.default_timeout_s
        review_agent = unit.review_agent if unit.review_agent else plan.default_review_agent

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
                if acceptance.custom_check:
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
    """Serialize a PlanSpec to TOML string.

    This allows the governor to build plans programmatically and persist them.
    """
    lines = [
        "[plan]",
        "version = 1",
        f'name = "{plan.name}"',
        f'goal = "{plan.goal}"',
    ]
    if plan.project_root != ".":
        lines.append(f'project_root = "{plan.project_root}"')
    if plan.session_root != ".":
        lines.append(f'session_root = "{plan.session_root}"')
    if plan.max_concurrent != 0:
        lines.append(f"max_concurrent = {plan.max_concurrent}")
    if plan.merge_strategy != "squash":
        lines.append(f'merge_strategy = "{plan.merge_strategy}"')
    if plan.default_agent != "qwen-9b":
        lines.append(f'default_agent = "{plan.default_agent}"')
    if plan.default_timeout_s != 600:
        lines.append(f"default_timeout_s = {plan.default_timeout_s}")
    if plan.permission_mode != "bypassPermissions":
        lines.append(f'permission_mode = "{plan.permission_mode}"')
    if plan.max_retries != 1:
        lines.append(f"max_retries = {plan.max_retries}")
    if plan.merge_resolve != "skip":
        lines.append(f'merge_resolve = "{plan.merge_resolve}"')
    if plan.default_review_agent:
        lines.append(f'default_review_agent = "{plan.default_review_agent}"')
    lines.append("")

    for plan_eval in plan.evals:
        lines.append("[[evals]]")
        lines.append(f'id = "{plan_eval.eval_id}"')
        lines.append(f'kind = "{plan_eval.kind}"')
        lines.append(f'statement = "{plan_eval.statement}"')
        lines.append(f'evidence = "{plan_eval.evidence}"')
        if plan_eval.scope:
            items = ", ".join(f'"{path}"' for path in plan_eval.scope)
            lines.append(f"scope = [{items}]")
        lines.append("")

    for slug, unit in plan.units.items():
        lines.append(f"[units.{slug}]")
        lines.append(f'summary = "{unit.summary}"')
        # Use triple-quoted strings for multi-line prompts
        if "\n" in unit.prompt:
            lines.append(f'prompt = """\n{unit.prompt}"""')
        else:
            lines.append(f'prompt = "{unit.prompt}"')
        lines.append(f'commit_message = "{unit.commit_message}"')
        if unit.agent:
            lines.append(f'agent = "{unit.agent}"')
        if unit.timeout_s:
            lines.append(f"timeout_s = {unit.timeout_s}")
        if unit.satisfies:
            items = ", ".join(f'"{eval_id}"' for eval_id in unit.satisfies)
            lines.append(f"satisfies = [{items}]")
        if unit.depends_on:
            deps = ", ".join(f'"{d}"' for d in unit.depends_on)
            lines.append(f"depends_on = [{deps}]")
        if unit.escalation:
            escs = ", ".join(f'"{e}"' for e in unit.escalation)
            lines.append(f"escalation = [{escs}]")
        if unit.review_agent:
            lines.append(f'review_agent = "{unit.review_agent}"')
        if unit.role != "worker":
            lines.append(f'role = "{unit.role}"')
        if unit.template:
            lines.append(f'template = "{unit.template}"')
        if unit.template_vars:
            lines.append("[units." + slug + ".vars]")
            for k, v in unit.template_vars.items():
                if "\n" in v:
                    lines.append(f'{k} = """\n{v}"""')
                else:
                    lines.append(f'{k} = "{v}"')

        lines.append("")

        # Files subtable
        has_files = unit.files.create or unit.files.edit or unit.files.delete or unit.files.read
        if has_files:
            lines.append(f"[units.{slug}.files]")
            if unit.files.create:
                items = ", ".join(f'"{f}"' for f in unit.files.create)
                lines.append(f"create = [{items}]")
            if unit.files.edit:
                items = ", ".join(f'"{f}"' for f in unit.files.edit)
                lines.append(f"edit = [{items}]")
            if unit.files.delete:
                items = ", ".join(f'"{f}"' for f in unit.files.delete)
                lines.append(f"delete = [{items}]")
            if unit.files.read:
                items = ", ".join(f'"{f}"' for f in unit.files.read)
                lines.append(f"read = [{items}]")
            lines.append("")

        # Acceptance subtable (only if non-default)
        acc = unit.acceptance
        default_acc = AcceptanceCriteria()
        if acc != default_acc:
            lines.append(f"[units.{slug}.acceptance]")
            if acc.tests_pass != default_acc.tests_pass:
                lines.append(f"tests_pass = {'true' if acc.tests_pass else 'false'}")
            if acc.lint_clean != default_acc.lint_clean:
                lines.append(f"lint_clean = {'true' if acc.lint_clean else 'false'}")
            if acc.custom_check:
                lines.append(f'custom_check = "{acc.custom_check}"')
            lines.append("")

    return "\n".join(lines) + "\n"


def run_plan(
    plan_file: str,
    *,
    session_root: str | None = None,
    max_concurrent: int = 0,
) -> object:
    """Canonical entry point: parse, validate, compile, execute a plan.

    Args:
        plan_file: Path to the TOML plan file.
        session_root: Session root (defaults to plan's session_root).
        max_concurrent: Override max concurrent workers (0=use plan default).

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
