"""Plan subcommands — validate, plan status, init-plan."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, print_dag_graph, resolve_plan_input, want_json
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

    issues = validate_plan(plan)
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


@cli.group(name="plan")
def plan_cmd() -> None:
    """Plan tree operations."""
    pass


@plan_cmd.command(name="status")
@click.argument("plan_input", type=click.Path(path_type=Path, exists=True))
def plan_status_cmd(plan_input: Path) -> None:
    """Show deployment status of a compiled plan.

    Accepts either a plan directory or a compiled TOML file. Reads
    `_compiled.toml` and `deployed.jsonl` to show pending vs deployed
    units, with a staleness warning when source TOMLs have changed
    (staleness is only checked when a plan directory is provided).

    \b
    Example: dgov plan status .dgov/plans/my-plan/
    Example: dgov plan status .dgov/plans/my-plan/_compiled.toml
    """
    try:
        compiled_path, plan_root = resolve_plan_input(plan_input)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        raise click.exceptions.Exit(code=1) from None
    _cmd_plan_status(compiled_path, plan_root)


def _cmd_plan_status(compiled_path: Path, plan_root: Path | None) -> None:
    """Pillar #4: Determinism — staleness detection prevents dispatching stale plans."""
    import tomllib

    from dgov.deploy_log import read as read_deploy_log
    from dgov.plan_tree import walk_tree

    if not compiled_path.exists():
        target = plan_root if plan_root is not None else compiled_path
        msg = f"Not compiled; run 'dgov compile {target}'"
        if want_json():
            click.echo(json.dumps({"status": "not_compiled", "message": msg}, indent=2))
        else:
            click.echo(msg)
        raise click.exceptions.Exit(code=1) from None

    raw = tomllib.loads(compiled_path.read_text())
    plan_section = raw.get("plan", {})
    plan_name = plan_section.get("name", "unknown")
    tasks_raw = raw.get("tasks", {})

    stale = False
    compiled_source_mtime = plan_section.get("source_mtime_max", "")
    if plan_root is not None:
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
            stale = current_mtime > (baseline_mtime + _MTIME_EPSILON_S)
        except (FileNotFoundError, ValueError):
            pass

    project_root = str(resolve_project_root())
    deployed = read_deploy_log(project_root, plan_name)
    deployed_units = {r.unit: r for r in deployed}

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

    deployed_count = sum(1 for u in unit_statuses if u["status"] == "deployed")
    pending_count = len(unit_statuses) - deployed_count

    if want_json():
        click.echo(
            json.dumps(
                {
                    "plan": plan_name,
                    "units": len(unit_statuses),
                    "deployed": deployed_count,
                    "pending": pending_count,
                    "stale": stale,
                    "unit_statuses": unit_statuses,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Plan: {plan_name}")
        total = len(unit_statuses)
        click.echo(f"Units: {total} total | {deployed_count} deployed | {pending_count} pending")
        if stale and plan_root is not None:
            click.echo(
                click.style(
                    f"  WARNING: compile stale; rerun 'dgov compile {plan_root}'",
                    fg="yellow",
                )
            )
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
