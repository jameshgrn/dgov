"""Plan subcommands — validate, plan status, init-plan."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.plan import parse_plan_file, validate_plan


def _format_unit_files(unit: object) -> str:
    """Render a PlanUnit's file claims as 'create=[...], edit=[...], delete=[...]'."""
    parts = []
    for kind in ("create", "edit", "delete"):
        files = getattr(unit.files, kind)  # type: ignore[attr-defined]
        if files:
            parts.append(f"{kind}={list(files)}")
    return ", ".join(parts)


def _echo_plan_summary(plan: object, errors: list, warnings: list) -> None:
    """Print human-readable plan summary with errors/warnings."""
    click.echo(f"Plan: {plan.name}")  # type: ignore[attr-defined]
    click.echo(f"Tasks: {len(plan.units)}")  # type: ignore[attr-defined]
    for slug, unit in plan.units.items():  # type: ignore[attr-defined]
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
@click.argument("plan_file", type=click.Path(path_type=Path, exists=True))
def validate_cmd(plan_file: Path) -> None:
    """Validate a plan file without running it.

    Parses the TOML, checks dependencies, detects file-claim conflicts,
    and prints a summary of the DAG.

    \b
    Example: dgov validate plan.toml
    """
    if plan_file.suffix != ".toml":
        click.echo(f"Error: Plan file must be .toml, got: {plan_file}", err=True)
        raise SystemExit(1)

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

    if errors:
        raise click.exceptions.Exit(code=1)


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

    \b
    Example: dgov init-plan my-plan --sections tasks,docs
    """
    plan_root = Path(".dgov") / "plans" / name

    if plan_root.exists() and not force:
        click.echo(f"Error: plan '{name}' already exists. Use --force to overwrite.", err=True)
        raise click.exceptions.Exit(code=1)

    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    if not section_list:
        section_list = ["tasks"]

    plan_root.mkdir(parents=True, exist_ok=force)
    for section in section_list:
        (plan_root / section).mkdir(exist_ok=True)

    sections_toml = ", ".join(f'"{s}"' for s in section_list)
    root_toml = f'''[plan]
name = "{name}"
summary = ""
sections = [{sections_toml}]
'''
    (plan_root / "_root.toml").write_text(root_toml)

    created = [str(plan_root), str(plan_root / "_root.toml")]
    for section in section_list:
        created.append(str(plan_root / section))

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
@click.argument("plan_root", type=click.Path(path_type=Path, exists=True, file_okay=False))
def plan_status_cmd(plan_root: Path) -> None:
    """Show deployment status of a compiled plan.

    Reads _compiled.toml and deployed.jsonl to show pending vs deployed
    units, with a staleness warning when source TOMLs have changed.

    \b
    Example: dgov plan status .dgov/plans/my-plan/
    """
    _cmd_plan_status(plan_root)


def _cmd_plan_status(plan_root: Path) -> None:
    """Pillar #4: Determinism — staleness detection prevents dispatching stale plans."""
    import tomllib

    from dgov.deploy_log import read as read_deploy_log
    from dgov.plan_tree import walk_tree

    compiled_path = plan_root / "_compiled.toml"

    if not compiled_path.exists():
        msg = f"Not compiled; run 'dgov compile {plan_root}'"
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
    compiled_mtime = compiled_path.stat().st_mtime
    try:
        tree = walk_tree(plan_root)
        current_mtime = max(
            (p.stat().st_mtime for paths in tree.section_files.values() for p in paths),
            default=0.0,
        )
        stale = current_mtime > compiled_mtime
    except (FileNotFoundError, ValueError):
        pass

    project_root = str(Path.cwd())
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
            unit_statuses.append(
                {
                    "unit": uid,
                    "status": "pending",
                    "blocked_by": ", ".join(blocked_by) if blocked_by else "",
                }
            )

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
        if stale:
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
