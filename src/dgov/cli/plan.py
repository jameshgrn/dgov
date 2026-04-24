"""Plan subcommands — validate, status, review, and remediation."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import click

from dgov.cli import cli, print_dag_graph, resolve_plan_input, want_json
from dgov.config import load_project_config
from dgov.deploy_log import DeployRecord
from dgov.plan import PlanSpec, PlanUnit, parse_plan_file, validate_plan
from dgov.plan_tree import parse_compiled_source_mtime
from dgov.project_root import resolve_project_root

_MTIME_EPSILON_S = 1e-6


def _format_unit_files(unit: PlanUnit) -> str:
    """Render a PlanUnit's file claims as 'files=[...], create=[...], etc.'."""
    parts = []
    for kind in ("touch", "create", "edit", "delete"):
        files = getattr(unit.files, kind)
        if files:
            label = "files" if kind == "touch" else kind
            parts.append(f"{label}={list(files)}")
    return ", ".join(parts)


def _echo_plan_summary(plan: PlanSpec, errors: list, warnings: list) -> None:
    """Print human-readable plan summary with errors/warnings."""
    click.echo(f"Plan: {plan.name}")
    click.echo(f"Tasks: {len(plan.units)}")
    for slug, unit in plan.units.items():
        deps = f" (depends: {', '.join(unit.depends_on)})" if unit.depends_on else ""
        click.echo(f"  - {slug}: {unit.summary}{deps}")
        files_str = _format_unit_files(unit)
        if files_str:
            click.echo(f"    files: {files_str}")

    if errors:
        click.echo("")
        for issue in errors:
            click.echo(f"  ERROR: {issue.message}", err=True)
    if warnings:
        click.echo("")
        for issue in warnings:
            click.echo(f"  WARNING: {issue.message}")

    if errors:
        click.echo(f"\nValidation FAILED ({len(errors)} error(s))")
    else:
        click.echo("\nValidation passed.")


@cli.command(name="validate")
@click.argument("plan_input", type=click.Path(path_type=Path, exists=True))
def validate_cmd(plan_input: Path) -> None:
    """Validate a plan without running it.

    Accepts either a plan directory (expects `_compiled.toml` inside) or a
    compiled TOML file directly. Parses the plan, checks dependencies,
    detects file-claim conflicts, and prints a summary of the DAG with
    its topology.

    \b
    Example: dgov validate .dgov/plans/my-plan/
    Example: dgov validate .dgov/plans/my-plan/_compiled.toml
    """
    try:
        plan_file, plan_dir = resolve_plan_input(plan_input)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        raise click.exceptions.Exit(code=1) from None

    if plan_dir is not None and not plan_file.exists():
        click.echo(
            f"Error: no _compiled.toml in {plan_dir}. Run 'dgov compile {plan_dir}' first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1) from None

    try:
        plan = parse_plan_file(str(plan_file))
    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None

    project_root_path = resolve_project_root(Path(plan_file))
    project_config = load_project_config(project_root_path)
    issues = validate_plan(plan, departments=project_config.departments)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if want_json():
        click.echo(
            json.dumps(
                {
                    "valid": len(errors) == 0,
                    "name": plan.name,
                    "tasks": len(plan.units),
                    "errors": [{"message": i.message, "unit": i.unit} for i in errors],
                    "warnings": [{"message": i.message, "unit": i.unit} for i in warnings],
                },
                indent=2,
            )
        )
    else:
        _echo_plan_summary(plan, errors, warnings)
        if not errors:
            print_dag_graph(plan.units)

    if errors:
        raise click.exceptions.Exit(code=1)


_EXAMPLE_UNIT_TOML = '''\
# Example unit — rename this file (remove the "_" prefix) and fill in the fields.
# The unit ID will be "<section>/<filename-stem>.<task-key>", e.g. "tasks/my-task.do-thing".

[tasks.do-thing]
summary = "One sentence: what does this task accomplish?"
prompt = """
Orient:
- Read `src/module/file.py` to understand the current structure.
- This task must NOT change any public interfaces.

Edit:
1. In `src/module/file.py`, add the new function after the existing helpers.
2. Use edit_file for targeted changes (prefer over write_file for existing files).

Verify:
- `uv run ruff check src/module/file.py`
- `uv run ruff format --check src/module/file.py`
- `uv run pytest -q -m unit tests/test_module.py`
"""
commit_message = "Imperative mood commit message (≤72 chars)"

# Declare file claims — use explicit intent over ambiguous shorthand.
# files.create = new files this task brings into existence
# files.edit   = existing files this task modifies
# files.read   = files needed for context but NOT modified (suppresses warnings)
# files.delete = files this task removes
files.edit = ["src/module/file.py"]
files.read = ["tests/test_module.py"]
# files.create = ["src/new_file.py"]
# Add more read-only context files as needed, e.g. ["src/module/types.py"]
# files.delete = ["src/old_file.py"]

# depends_on = ["other-section/other-file.other-task"]
# agent = "accounts/fireworks/routers/kimi-k2p5-turbo"
'''


@cli.command(name="init-plan")
@click.argument("name")
@click.option(
    "--sections",
    default="tasks",
    help="Comma-separated list of sections to create",
)
@click.option("--force", is_flag=True, help="Overwrite existing plan directory")
def init_plan_cmd(name: str, sections: str, force: bool) -> None:
    """Initialize a new plan with directory structure.

    Creates .dgov/plans/<name>/ with _root.toml and section directories.
    Each section gets a _example.toml showing the unit format.
    Copy or rename it before compile; underscore-prefixed files are ignored.

    \b
    Example: dgov init-plan my-plan --sections tasks,docs
    """
    project_root = resolve_project_root()
    plan_root = project_root / ".dgov" / "plans" / name

    if plan_root.exists() and not force:
        click.echo(f"Error: plan '{name}' already exists. Use --force to overwrite.", err=True)
        raise click.exceptions.Exit(code=1)

    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    if not section_list:
        section_list = ["tasks"]

    plan_root.mkdir(parents=True, exist_ok=force)
    for section in section_list:
        (plan_root / section).mkdir(exist_ok=True)
        (plan_root / section / "_example.toml").write_text(_EXAMPLE_UNIT_TOML)

    sections_toml = ", ".join(f'"{s}"' for s in section_list)
    root_toml = f'''[plan]
name = "{name}"
summary = ""  # One sentence describing what this plan accomplishes
sections = [{sections_toml}]
'''
    (plan_root / "_root.toml").write_text(root_toml)

    created = [str(plan_root), str(plan_root / "_root.toml")]
    for section in section_list:
        created.append(str(plan_root / section))
        created.append(str(plan_root / section / "_example.toml"))

    if want_json():
        click.echo(
            json.dumps(
                {
                    "status": "initialized",
                    "name": name,
                    "root": str(plan_root),
                    "sections": section_list,
                    "created": created,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Initialized plan '{name}':")
        for path in created:
            click.echo(f"  {path}")
        click.echo(
            "Next: copy or rename each _example.toml to a non-underscore filename before "
            "running compile."
        )


@cli.group(name="plan")
def plan_cmd() -> None:
    """Plan tree operations."""
    pass


@plan_cmd.command(name="status")
@click.argument("plan_input", type=click.Path(path_type=Path))
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show the per-unit list (default: one-line summary)",
)
def plan_status_cmd(plan_input: Path, verbose: bool) -> None:
    """Show deployment status of a compiled plan.

    Default output is a one-line summary: plan name, N/M deployed, and
    a staleness warning when the compile is out of date. Pass `--verbose`
    to see each unit with its deploy sha or blocked-by chain. Use
    `dgov plan review` for the rich per-unit debrief.

    Accepts either a plan directory or a compiled TOML file.

    \b
    Example: dgov plan status .dgov/plans/my-plan/
    Example: dgov plan status .dgov/plans/my-plan/ --verbose
    """
    plan_input = _resolve_archived_plan_path(plan_input)
    if not plan_input.exists():
        click.echo(f"Error: plan path not found: {plan_input}", err=True)
        raise click.exceptions.Exit(code=1) from None
    try:
        compiled_path, plan_root = resolve_plan_input(plan_input)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        raise click.exceptions.Exit(code=1) from None
    _cmd_plan_status(compiled_path, plan_root, verbose=verbose)


def _check_compiled_exists(compiled_path: Path, plan_root: Path | None) -> None:
    """Handle the not-compiled error path. Raises ClickException on failure."""
    if not compiled_path.exists():
        target = plan_root if plan_root is not None else compiled_path
        msg = f"Not compiled; run 'dgov compile {target}'"
        if want_json():
            click.echo(json.dumps({"status": "not_compiled", "message": msg}, indent=2))
        else:
            click.echo(msg)
        raise click.exceptions.Exit(code=1) from None


def _compute_staleness(
    compiled_path: Path, plan_root: Path | None, compiled_source_mtime: str
) -> bool:
    """Compute staleness by comparing source mtime against compiled baseline."""
    from dgov.plan_tree import walk_tree

    if plan_root is None:
        return False
    try:
        tree = walk_tree(plan_root)
        current_mtime = max(
            (plan_root / "_root.toml").stat().st_mtime,
            *(p.stat().st_mtime for paths in tree.section_files.values() for p in paths),
        )
        baseline_mtime = (
            parse_compiled_source_mtime(compiled_source_mtime)
            if isinstance(compiled_source_mtime, str) and compiled_source_mtime
            else compiled_path.stat().st_mtime
        )
        return current_mtime > (baseline_mtime + _MTIME_EPSILON_S)
    except (FileNotFoundError, ValueError):
        return False


def _load_deployed_units(project_root: Path, plan_name: str) -> dict[str, DeployRecord]:
    """Load deployed units from the deploy log. Returns a dict of unit -> deploy record."""
    from dgov.deploy_log import read as read_deploy_log

    deployed = read_deploy_log(str(project_root), plan_name)
    return {r.unit: r for r in deployed}


def _build_unit_statuses(
    tasks_raw: dict, deployed_units: dict[str, DeployRecord]
) -> list[dict[str, str]]:
    """Build the list of unit status dicts from tasks and deployed records."""
    unit_statuses: list[dict[str, str]] = []
    for uid in sorted(tasks_raw):
        if uid in deployed_units:
            r = deployed_units[uid]
            unit_statuses.append({"unit": uid, "status": "deployed", "sha": r.sha, "ts": r.ts})
        else:
            task = tasks_raw[uid]
            deps = task.get("depends_on", [])
            blocked_by = [d for d in deps if d not in deployed_units]
            unit_statuses.append({
                "unit": uid,
                "status": "pending",
                "blocked_by": ", ".join(blocked_by) if blocked_by else "",
            })
    return unit_statuses


def _render_plan_status_json(
    plan_name: str,
    unit_statuses: list[dict[str, str]],
    deployed_count: int,
    pending_count: int,
    stale: bool,
    run_status: str | None,
    sentrux_degradation: bool | None,
    sentrux_offender_summary: str | None,
    remediation_needed: bool,
    next_action: str | None,
) -> None:
    """Render plan status as JSON output."""
    click.echo(
        json.dumps(
            {
                "plan": plan_name,
                "units": len(unit_statuses),
                "deployed": deployed_count,
                "pending": pending_count,
                "stale": stale,
                "run_status": run_status,
                "sentrux_degradation": sentrux_degradation,
                "sentrux_offender_summary": sentrux_offender_summary,
                "remediation_needed": remediation_needed,
                "next_action": next_action,
                "unit_statuses": unit_statuses,
            },
            indent=2,
        )
    )


def _render_plan_status_text(
    plan_name: str,
    unit_statuses: list[dict[str, str]],
    deployed_count: int,
    pending_count: int,
    stale: bool,
    plan_root: Path | None,
    verbose: bool,
    run_status: str | None,
    sentrux_advisory: str | None,
    remediation_needed: bool,
    next_action: str | None,
) -> None:
    """Render plan status as human-readable text output."""
    total = len(unit_statuses)
    # One-line summary by default. Deep-dive via `dgov plan review`.
    click.echo(f"Plan: {plan_name}  ({deployed_count}/{total} deployed, {pending_count} pending)")
    if run_status is not None:
        color = _run_status_color(run_status)
        line = f"  run status: {run_status}"
        click.echo(click.style(line, fg=color) if color else line)
    if sentrux_advisory is not None:
        click.echo(click.style(f"  advisory: {sentrux_advisory}", fg="yellow"))
    if remediation_needed and next_action is not None:
        click.echo(
            click.style(
                "  deployed but unresolved — author a remediation follow-up",
                fg="yellow",
            )
        )
        click.echo(click.style(f"  next: {next_action}", fg="yellow"))
    if stale and plan_root is not None:
        click.echo(
            click.style(
                f"  stale — rerun 'dgov compile {plan_root}'",
                fg="yellow",
            )
        )
    if verbose:
        click.echo("")
        for u in unit_statuses:
            if u["status"] == "deployed":
                line = f"  {click.style('✓', fg='green')} {u['unit']}"
                line += f"  (deployed {u['ts']}, sha {u['sha'][:7]})"
            else:
                line = f"  ○ {u['unit']}"
                if u.get("blocked_by"):
                    line += f"  (pending, blocked by: {u['blocked_by']})"
                else:
                    line += "  (pending)"
            click.echo(line)


def _needs_remediation(
    *, run_status: str | None, unit_count: int, deployed_count: int, pending_count: int
) -> bool:
    """Return True when a plan is fully deployed but still degraded."""
    return (
        run_status == "degraded"
        and unit_count > 0
        and deployed_count == unit_count
        and pending_count == 0
    )


def _status_target(plan_root: Path | None, compiled_path: Path) -> str:
    """Return the display target users should pass back to plan commands."""
    return str(plan_root if plan_root is not None else compiled_path)


def _cmd_plan_status(
    compiled_path: Path, plan_root: Path | None, *, verbose: bool = False
) -> None:
    """Pillar #4: Determinism — staleness detection prevents dispatching stale plans."""
    import tomllib

    _check_compiled_exists(compiled_path, plan_root)

    raw = tomllib.loads(compiled_path.read_text())
    plan_section = raw.get("plan", {})
    plan_name = plan_section.get("name", "unknown")
    tasks_raw = raw.get("tasks", {})
    project_root_path = resolve_project_root(compiled_path)

    compiled_source_mtime = plan_section.get("source_mtime_max", "")
    stale = _compute_staleness(compiled_path, plan_root, compiled_source_mtime)

    deployed_units = _load_deployed_units(project_root_path, plan_name)
    unit_statuses = _build_unit_statuses(tasks_raw, deployed_units)
    deployed_count = sum(1 for u in unit_statuses if u["status"] == "deployed")
    pending_count = len(unit_statuses) - deployed_count
    from dgov.plan_review import load_run_envelope

    run_envelope = load_run_envelope(str(project_root_path), compiled_path)
    remediation_needed = _needs_remediation(
        run_status=run_envelope.run_status,
        unit_count=len(unit_statuses),
        deployed_count=deployed_count,
        pending_count=pending_count,
    )
    next_action = None
    if remediation_needed:
        next_action = f"dgov plan remediate {_status_target(plan_root, compiled_path)}"

    if want_json():
        _render_plan_status_json(
            plan_name,
            unit_statuses,
            deployed_count,
            pending_count,
            stale,
            run_envelope.run_status,
            run_envelope.sentrux_degradation,
            run_envelope.sentrux_offender_summary,
            remediation_needed,
            next_action,
        )
    else:
        _render_plan_status_text(
            plan_name,
            unit_statuses,
            deployed_count,
            pending_count,
            stale,
            plan_root,
            verbose,
            run_envelope.run_status,
            _format_sentrux_advisory(run_envelope),
            remediation_needed,
            next_action,
        )


def _resolve_archived_plan_path(plan_input: Path) -> Path:
    """If plan_input does not exist, look for it under a sibling archive/ dir.

    Auto-archive (after a fully-deployed run) moves a plan from
    `.dgov/plans/<name>/` to `.dgov/plans/archive/<name>/`. Point a user who
    passes the original path at the archived copy, with a note on stderr so
    the redirect is visible.

    Returns the resolved path, or the original `plan_input` if no archive
    candidate is found (callers still have to check `exists()` themselves).
    """
    if plan_input.exists():
        return plan_input
    # Candidate: `<parent>/archive/<name>` for a directory input, or
    # `<parent-parent>/archive/<parent-name>/<file>` for a file input.
    if plan_input.suffix == "":
        candidate = plan_input.parent / "archive" / plan_input.name
    else:
        candidate = plan_input.parent.parent / "archive" / plan_input.parent.name / plan_input.name
    if candidate.exists():
        click.echo(
            f"note: '{plan_input}' not found — resolved to archived plan at {candidate}",
            err=True,
        )
        return candidate
    return plan_input


def _slugify_name(text: str) -> str:
    """Convert text into a stable kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:50] or "plan"


def _allocate_plan_dir(
    project_root: Path, base_name: str, *, explicit_name: bool = False
) -> tuple[str, Path]:
    """Allocate a remediation plan dir with exact-or-fail explicit naming."""
    plans_dir = project_root / ".dgov" / "plans"
    archive_dir = plans_dir / "archive"
    live_plan_dir = plans_dir / base_name
    archive_plan_dir = archive_dir / base_name
    if explicit_name and (live_plan_dir.exists() or archive_plan_dir.exists()):
        existing_path = live_plan_dir if live_plan_dir.exists() else archive_plan_dir
        click.echo(f"Error: remediation plan already exists at {existing_path}", err=True)
        click.echo(
            "Fix: use a different --name or archive/remove the existing remediation plan.",
            err=True,
        )
        raise click.exceptions.Exit(code=1) from None
    if live_plan_dir.exists():
        click.echo(f"Error: remediation plan already exists at {live_plan_dir}", err=True)
        click.echo(
            "Fix: use that active remediation plan or archive it before creating another.",
            err=True,
        )
        raise click.exceptions.Exit(code=1) from None

    name = base_name
    plan_dir = live_plan_dir
    suffix = 2
    while plan_dir.exists() or (archive_dir / name).exists():
        name = f"{base_name}-{suffix}"
        plan_dir = plans_dir / name
        suffix += 1
    return name, plan_dir


def _toml_str(value: str) -> str:
    """Render a TOML-safe double-quoted string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _render_remediation_context(review, source_target: str) -> str:
    """Build markdown context for a remediation scaffold."""
    lines = [
        "# Remediation Context",
        "",
        f"- Source plan: `{review.plan_name}`",
        f"- Source path: `{source_target}`",
        f"- Generated: `{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        f"- Last run status: `{review.run_status or 'unknown'}`",
    ]
    advisory = _format_sentrux_advisory(review)
    if advisory is not None:
        lines.append(f"- Advisory: {advisory}")
    lines.extend(
        [
            "",
            "Use this context to author concrete remediation tasks with explicit file claims.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_remediation_example(review, source_target: str) -> str:
    """Render the starter task scaffold for a remediation plan."""
    advisory = _format_sentrux_advisory(review) or "sentrux degradation detected"
    return (
        "# Rename this file to a non-underscore name once you replace the scaffold.\n\n"
        "[tasks.remediate]\n"
        f'summary = {_toml_str(f"Address degradation from {review.plan_name}")}\n'
        'prompt = """\n'
        "Orient:\n"
        f"- Read `dgov plan review {source_target}` for the merged units and run-level advisory.\n"
        "- Run `uv run dgov sentrux offenders` before editing anything.\n"
        "- This follow-up must stay narrowly scoped to the degradation it is addressing.\n\n"
        "Edit:\n"
        "1. Replace this scaffold with concrete remediation steps for the exact offending files.\n"
        "2. Keep tasks atomic; split separate offender groups into separate tasks if needed.\n"
        f"3. Preserve the intent of the source plan while fixing this advisory: {advisory}.\n\n"
        "Verify:\n"
        "- `uv run dgov sentrux offenders`\n"
        "- Add targeted `uv run pytest -q ...` commands for the changed files.\n"
        "- Add targeted `uv run ruff check ...` and `uv run ty check ...` commands.\n"
        '"""\n'
        f'commit_message = {_toml_str(f"Address degradation from {review.plan_name}")}\n'
        "# files.edit = [\"src/path.py\"]\n"
        "# files.read = [\"tests/test_path.py\"]\n"
    )


@plan_cmd.command(name="remediate")
@click.argument("plan_input", type=click.Path(path_type=Path))
@click.option("--name", help="Override the generated remediation plan name")
def plan_remediate_cmd(plan_input: Path, name: str | None) -> None:
    """Scaffold a follow-up plan for a fully deployed but degraded source plan."""
    plan_input = _resolve_archived_plan_path(plan_input)
    if not plan_input.exists():
        click.echo(f"Error: plan path not found: {plan_input}", err=True)
        raise click.exceptions.Exit(code=1) from None
    try:
        compiled_path, plan_root = resolve_plan_input(plan_input)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        raise click.exceptions.Exit(code=1) from None

    _check_compiled_exists(compiled_path, plan_root)
    import tomllib

    from dgov.plan_review import load_run_envelope

    raw = tomllib.loads(compiled_path.read_text())
    tasks_raw = raw.get("tasks", {})
    project_root = resolve_project_root(compiled_path)
    deployed_units = _load_deployed_units(project_root, raw.get("plan", {}).get("name", "unknown"))
    deployed_count = sum(1 for uid in tasks_raw if uid in deployed_units)
    pending_count = len(tasks_raw) - deployed_count
    run_envelope = load_run_envelope(str(project_root), compiled_path)
    if not _needs_remediation(
        run_status=run_envelope.run_status,
        unit_count=len(tasks_raw),
        deployed_count=deployed_count,
        pending_count=pending_count,
    ):
        click.echo(
            "Error: remediation scaffolds only apply to fully deployed plans whose last run "
            "status is degraded.",
            err=True,
        )
        raise click.exceptions.Exit(code=1) from None

    base_name = name or f"{_slugify_name(run_envelope.plan_name)}-remediation"
    plan_name, plan_dir = _allocate_plan_dir(
        project_root, base_name, explicit_name=name is not None
    )
    source_target = _status_target(plan_root, compiled_path)

    plan_dir.mkdir(parents=True)
    fix_dir = plan_dir / "fix"
    fix_dir.mkdir()
    (plan_dir / "_root.toml").write_text(
        "[plan]\n"
        f"name = {_toml_str(plan_name)}\n"
        f"summary = {_toml_str(f'Remediate degradation from {run_envelope.plan_name}')}\n"
        'sections = ["fix"]\n'
    )
    (fix_dir / "_context.md").write_text(
        _render_remediation_context(run_envelope, source_target)
    )
    (fix_dir / "_example.toml").write_text(
        _render_remediation_example(run_envelope, source_target)
    )

    click.echo(f"Created remediation plan '{plan_name}' at {plan_dir}")
    click.echo(f"Next: replace {fix_dir / '_example.toml'} with concrete tasks, then compile.")


@plan_cmd.command(name="review")
@click.argument("plan_input", type=click.Path(path_type=Path))
@click.option("--only", default=None, help="Review only this exact unit id")
@click.option(
    "--diff",
    "diff_unit",
    default=None,
    help="Print the full git show diff for this unit (exact match)",
)
@click.option(
    "--events",
    "events_unit",
    default=None,
    help="Print the full worker activity timeline for this unit (exact match)",
)
def plan_review_cmd(
    plan_input: Path,
    only: str | None,
    diff_unit: str | None,
    events_unit: str | None,
) -> None:
    """Post-hoc debrief of the last dgov run for a plan.

    Shows what landed, how hard each worker worked to land it, and the
    reject reason with a hint when settlement failed. Scopes to the last
    run via the run_start marker.

    Accepts either a live plan directory or an archived one. If the live
    path does not exist but an archive copy is found, the debrief resolves
    to the archive automatically and prints a note to stderr.

    \b
    Example: dgov plan review .dgov/plans/my-plan/
    Example: dgov plan review my-plan/ --only tasks/main.thing
    Example: dgov plan review my-plan/ --diff tasks/main.thing --events tasks/main.thing
    """
    plan_input = _resolve_archived_plan_path(plan_input)
    if not plan_input.exists():
        click.echo(f"Error: plan path not found: {plan_input}", err=True)
        raise click.exceptions.Exit(code=1) from None
    try:
        compiled_path, plan_root = resolve_plan_input(plan_input)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        raise click.exceptions.Exit(code=1) from None
    _cmd_plan_review(
        compiled_path,
        plan_root,
        only=only,
        diff_unit=diff_unit,
        events_unit=events_unit,
    )


def _cmd_plan_review(
    compiled_path: Path,
    plan_root: Path | None,
    *,
    only: str | None,
    diff_unit: str | None,
    events_unit: str | None,
) -> None:
    """Build and render a PlanReview."""
    from dgov.config import load_project_config
    from dgov.plan_review import load_review

    if not compiled_path.exists():
        target = plan_root if plan_root is not None else compiled_path
        msg = f"Not compiled; run 'dgov compile {target}'"
        if want_json():
            click.echo(json.dumps({"status": "not_compiled", "message": msg}, indent=2))
        else:
            click.echo(msg)
        raise click.exceptions.Exit(code=1) from None

    project_root_path = resolve_project_root()
    project_config = load_project_config(project_root_path)
    include_full_diff = diff_unit is not None

    review = load_review(
        project_root=str(project_root_path),
        compiled_path=compiled_path,
        plan_dir=plan_root,
        only=only,
        include_full_diff=include_full_diff,
        iteration_budget=project_config.worker_iteration_budget,
    )

    if only is not None and not review.units:
        click.echo(
            click.style(
                f"Error: no unit matches --only {only}. Use exact unit id from "
                f"`dgov plan status {plan_root or compiled_path}`.",
                fg="red",
            ),
            err=True,
        )
        raise click.exceptions.Exit(code=1) from None

    if want_json():
        click.echo(_review_to_json(review))
        return

    _render_review_human(review, diff_unit=diff_unit, events_unit=events_unit)

    if review.failed_count > 0:
        raise click.exceptions.Exit(code=1)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "(unknown)"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(seconds, 60)
    return f"{int(minutes)}m {rem:.0f}s"


def _run_status_color(run_status: str) -> str | None:
    """Return display color for run-level status."""
    if run_status == "failed":
        return "red"
    if run_status in ("degraded", "partial"):
        return "yellow"
    return None


def _format_sentrux_advisory(review) -> str | None:
    """Build the run-level Sentrux advisory line for human review output."""
    if not review.sentrux_degradation:
        return None
    advisory_parts = ["sentrux degradation detected"]
    if review.sentrux_quality_before is not None and review.sentrux_quality_after is not None:
        before, after = review.sentrux_quality_before, review.sentrux_quality_after
        advisory_parts.append(f"quality {before} -> {after}")
    if review.sentrux_offender_summary:
        advisory_parts.append(review.sentrux_offender_summary)
    if review.sentrux_error:
        advisory_parts.append(f"error: {review.sentrux_error}")
    return "; ".join(advisory_parts)


def _render_run_advisory(review) -> None:
    """Render run-level status and Sentrux advisory before unit details."""
    if review.run_status:
        status_line = f"  run status: {review.run_status}"
        color = _run_status_color(review.run_status)
        click.echo(click.style(status_line, fg=color) if color else status_line)
    sentrux_advisory = _format_sentrux_advisory(review)
    if sentrux_advisory:
        click.echo(click.style(f"  advisory: {sentrux_advisory}", fg="yellow"))


def _render_review_human(review, *, diff_unit: str | None, events_unit: str | None) -> None:
    """Render a PlanReview for a human. Not pure — writes to stdout via click.echo."""
    click.echo(f"Plan: {review.plan_name}")
    if review.source_dir is not None:
        click.echo(f"  source: {review.source_dir}")
    if review.last_run_ts:
        dur_part = ""
        if review.last_run_duration_s:
            dur_part = f" ({_fmt_duration(review.last_run_duration_s)})"
        click.echo(f"  last run: {review.last_run_ts}{dur_part}")
    _render_run_advisory(review)

    total = len(review.units)
    click.echo("")
    click.echo(
        f"Units: {review.deployed_count}/{total} deployed"
        f" | {review.active_count} active"
        f" | {review.pending_count} pending"
        f" | {review.failed_count} failed"
    )
    click.echo("")

    for unit in review.units:
        _render_unit(unit)
        if diff_unit is not None and unit.unit == diff_unit:
            _render_unit_diff(unit)
        if events_unit is not None and unit.unit == events_unit:
            _render_unit_events(unit)

    if diff_unit is not None and not any(u.unit == diff_unit for u in review.units):
        click.echo(
            click.style(f"  (no unit matches --diff {diff_unit})", fg="yellow"),
            err=True,
        )
    if events_unit is not None and not any(u.unit == events_unit for u in review.units):
        click.echo(
            click.style(f"  (no unit matches --events {events_unit})", fg="yellow"),
            err=True,
        )


def _render_unit_fields(fields: list[tuple[str, str | None]]) -> None:
    """Render a list of label/value pairs with consistent formatting."""
    for label, value in fields:
        if value is not None:
            click.echo(f"    {label:12s} {value}")


def _render_deployed_unit(unit) -> None:
    """Render a deployed UnitReview block."""
    marker = click.style("✓", fg="green")
    header = f"  {marker} {unit.unit}"
    click.echo(header)
    fields: list[tuple[str, str | None]] = [
        ("task", unit.summary),
        (
            "commit",
            f"{unit.commit_sha[:8]} — {unit.commit_message}"
            if unit.commit_sha and unit.commit_message
            else (unit.commit_sha[:8] if unit.commit_sha else None),
        ),
        ("agent", unit.agent),
        ("diff", unit.diff_stat.summary() if unit.diff_stat is not None else None),
        ("duration", _fmt_duration(unit.duration_s) if unit.duration_s is not None else None),
        (
            "iterations",
            f"{unit.iterations} tool call{'s' if unit.iterations != 1 else ''}"
            if unit.iterations is not None
            else None,
        ),
        (
            "settlement",
            {"ok": "ok (first try)", "ok_retried": "ok (after retry)"}.get(
                unit.settlement, unit.settlement
            )
            if unit.settlement != "n/a"
            else None,
        ),
        (
            "tokens",
            f"{unit.prompt_tokens:,} prompt + {unit.completion_tokens:,} completion"
            if unit.prompt_tokens is not None and unit.completion_tokens is not None
            else None,
        ),
    ]
    _render_unit_fields(fields)
    if unit.landed_files:
        _render_path_list("files       ", unit.landed_files)
    if unit.self_corrections > 0:
        click.echo(
            f"    self-correct {unit.self_corrections} failed tool call(s) recovered before done"
        )
    if unit.fork_depth > 0:
        click.echo(f"    fork         {unit.fork_depth} clean-context relaunch(es)")
    if unit.self_review_outcome is not None:
        _sr_labels = {
            "passed": ("self-review passed", "green"),
            "rejected": ("self-review rejected → fix applied", "yellow"),
            "auto_passed": ("self-review rejected → auto-passed after fix", "yellow"),
            "error": ("self-review error → auto-passed", "red"),
        }
        label, color = _sr_labels.get(
            unit.self_review_outcome,
            (f"self-review: {unit.self_review_outcome}", None),
        )
        click.echo(click.style(f"    self-review  {label}", fg=color))
    _render_integration_telemetry(unit)
    if unit.done_summary:
        _render_multiline_field("worker note ", unit.done_summary)
    if unit.worker_note_mismatches:
        mismatch_list = ", ".join(unit.worker_note_mismatches)
        click.echo(
            click.style(
                f"    warning      worker note mentions files not in landed diff: {mismatch_list}",
                fg="yellow",
            )
        )
    click.echo("")


def _render_integration_telemetry(unit) -> None:
    """Render integration risk and candidate validation telemetry if present."""
    has_risk_level = unit.integration_risk_level and unit.integration_risk_level != "none"
    if has_risk_level or unit.integration_risk_detected:
        if has_risk_level:
            risk_label = unit.integration_risk_level
            if unit.integration_risk_detected:
                risk_label += ", overlap detected"
            click.echo(click.style(f"    integration  risk={risk_label}", fg="yellow"))
        else:
            click.echo(click.style("    integration  overlap detected", fg="yellow"))
    if unit.integration_candidate_passed is True:
        click.echo("    candidate    passed")
    elif unit.integration_candidate_passed is False:
        fc = unit.integration_failure_class or "failed"
        click.echo(click.style(f"    candidate    {fc}", fg="red"))


def _render_failed_unit(unit) -> None:
    """Render a failed UnitReview block."""
    marker = click.style("✗", fg="red")
    where = unit.reject_verdict or "worker error"
    click.echo(f"  {marker} {unit.unit}  (failed: {where})")
    fields: list[tuple[str, str | None]] = [
        ("agent", unit.agent),
        ("attempts", str(unit.attempts) if unit.attempts > 1 else None),
        ("duration", _fmt_duration(unit.duration_s) if unit.duration_s is not None else None),
        (
            "iterations",
            f"{unit.iterations} tool call{'s' if unit.iterations != 1 else ''}"
            if unit.iterations is not None
            else None,
        ),
        ("reject", unit.reject_verdict),
        (
            "fork",
            f"{unit.fork_depth} clean-context relaunch(es)" if unit.fork_depth > 0 else None,
        ),
        (
            "tokens",
            f"{unit.prompt_tokens:,} prompt + {unit.completion_tokens:,} completion"
            if unit.prompt_tokens is not None and unit.completion_tokens is not None
            else None,
        ),
    ]
    _render_unit_fields(fields)
    _render_integration_telemetry(unit)
    if unit.error:
        _render_multiline_field("error       ", unit.error)
    if unit.last_thought:
        _render_multiline_field("last thought", unit.last_thought, max_lines=2)
    if unit.hint:
        click.echo(click.style(f"    hint         {unit.hint}", fg="yellow"))
    click.echo("")


def _render_active_unit(unit) -> None:
    """Render an in-flight UnitReview block."""
    marker = click.style("…", fg="cyan")
    click.echo(f"  {marker} {unit.unit}  (active)")
    fields: list[tuple[str, str | None]] = [
        ("task", unit.summary),
        ("agent", unit.agent),
        ("duration", _fmt_duration(unit.duration_s) if unit.duration_s is not None else None),
        (
            "iterations",
            f"{unit.iterations} tool call{'s' if unit.iterations != 1 else ''}"
            if unit.iterations is not None
            else None,
        ),
    ]
    _render_unit_fields(fields)
    if unit.last_thought:
        _render_multiline_field("last thought", unit.last_thought, max_lines=2)
    click.echo("")


def _render_pending_unit(unit) -> None:
    """Render a pending/not_run UnitReview block."""
    marker = click.style("○", dim=True)
    click.echo(f"  {marker} {unit.unit}  (not run in this window)")
    if unit.summary:
        click.echo(f"    {click.style(unit.summary, dim=True)}")
    click.echo("")


# Dispatch mapping from unit status to renderer function.
_UNIT_STATUS_RENDERERS = {
    "deployed": _render_deployed_unit,
    "failed": _render_failed_unit,
    "active": _render_active_unit,
    "pending": _render_pending_unit,
    "not_run": _render_pending_unit,
}


def _render_unit(unit) -> None:
    """Render a single UnitReview block. Shape depends on status."""
    renderer = _UNIT_STATUS_RENDERERS.get(unit.status)
    if renderer is None:
        # Defensive: unknown status falls back to pending rendering.
        renderer = _render_pending_unit
    renderer(unit)


def _render_multiline_field(label: str, text: str, max_lines: int = 4) -> None:
    """Render a multi-line field with the label on the first line and continuation indent."""
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], "…"]
    click.echo(f"    {label} {lines[0]}")
    for line in lines[1:]:
        click.echo(f"                 {line}")


def _render_path_list(label: str, paths: tuple[str, ...], max_items: int = 5) -> None:
    """Render an ordered path list with truncation."""
    items = [path for path in paths if path]
    if not items:
        return
    display = items[:max_items]
    if len(items) > max_items:
        display.append(f"... +{len(items) - max_items} more")
    click.echo(f"    {label} {display[0]}")
    for path in display[1:]:
        click.echo(f"                 {path}")


def _render_unit_diff(unit) -> None:
    click.echo(click.style(f"    --- diff for {unit.unit} ---", dim=True))
    if unit.full_diff is None:
        click.echo(click.style("    (no diff available)", dim=True))
    else:
        for line in unit.full_diff.splitlines():
            click.echo(f"    {line}")
    click.echo("")


def _render_unit_events(unit) -> None:
    click.echo(click.style(f"    --- activity for {unit.unit} ---", dim=True))
    if not unit.activity:
        click.echo(click.style("    (no worker tool calls recorded)", dim=True))
    else:
        for call in unit.activity:
            tool = call.get("tool", "?")
            args = call.get("args", {})
            arg_preview = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            click.echo(f"    {tool}({arg_preview})")
    if unit.thoughts:
        click.echo(click.style("    thoughts:", dim=True))
        for thought in unit.thoughts:
            first_line = thought.splitlines()[0] if thought else ""
            click.echo(f"      · {first_line[:100]}")
    click.echo("")


def _review_to_json(review) -> str:
    """Serialize a PlanReview to indented JSON."""

    def _unit_dict(u) -> dict:
        return {
            "unit": u.unit,
            "summary": u.summary,
            "status": u.status,
            "agent": u.agent,
            "commit_sha": u.commit_sha,
            "commit_message": u.commit_message,
            "commit_ts": u.commit_ts,
            "diff_stat": {
                "files_changed": u.diff_stat.files_changed,
                "insertions": u.diff_stat.insertions,
                "deletions": u.diff_stat.deletions,
            }
            if u.diff_stat is not None
            else None,
            "landed_files": list(u.landed_files),
            "full_diff": u.full_diff,
            "duration_s": u.duration_s,
            "iterations": u.iterations,
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "self_corrections": u.self_corrections,
            "fork_depth": u.fork_depth,
            "self_review_outcome": u.self_review_outcome,
            "attempts": u.attempts,
            "settlement": u.settlement,
            "done_summary": u.done_summary,
            "worker_note_mismatches": list(u.worker_note_mismatches),
            "thoughts": list(u.thoughts),
            "activity": [dict(call) for call in u.activity],
            "reject_verdict": u.reject_verdict,
            "error": u.error,
            "last_thought": u.last_thought,
            "hint": u.hint,
            # Integration risk telemetry
            "integration_risk_level": u.integration_risk_level,
            "integration_risk_detected": u.integration_risk_detected,
            "integration_candidate_passed": u.integration_candidate_passed,
            "integration_failure_class": u.integration_failure_class,
        }

    return json.dumps(
        {
            "plan": review.plan_name,
            "source_dir": str(review.source_dir) if review.source_dir else None,
            "last_run_ts": review.last_run_ts,
            "last_run_duration_s": review.last_run_duration_s,
            "run_status": review.run_status,
            "sentrux_degradation": review.sentrux_degradation,
            "sentrux_quality_before": review.sentrux_quality_before,
            "sentrux_quality_after": review.sentrux_quality_after,
            "sentrux_error": review.sentrux_error,
            "sentrux_offender_summary": review.sentrux_offender_summary,
            "deployed": review.deployed_count,
            "active": review.active_count,
            "failed": review.failed_count,
            "pending": review.pending_count,
            "units": [_unit_dict(u) for u in review.units],
        },
        indent=2,
        default=str,
    )


# Register plan create subcommand — must be at bottom after plan_cmd is defined
from dgov.cli import plan_create as plan_create  # noqa: E402
