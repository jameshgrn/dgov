"""CLI commands for dgov plan execution."""

from __future__ import annotations

import json
import os

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group("plan")
def plan_cmd():
    """Plan management: validate, compile, and run structured plans."""
    pass


@plan_cmd.command("scratch")
@click.argument("name")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--force", is_flag=True, help="Overwrite an existing scratch plan")
def plan_scratch(name, project_root, session_root, force):
    """Create a scratch plan under .dgov/plans/."""
    from dgov.plan import write_scratch_plan

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root

    try:
        path = write_scratch_plan(
            name,
            project_root=project_root,
            session_root=session_root,
            force=force,
        )
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    click.echo(str(path))


@plan_cmd.command("validate")
@click.argument("plan_file", type=click.Path(exists=True))
def plan_validate(plan_file):
    """Validate a plan TOML file and print any issues."""
    from dgov.plan import parse_plan_file, validate_plan

    try:
        plan = parse_plan_file(plan_file)
    except ValueError as e:
        click.secho(f"Parse error: {e}", fg="red")
        raise SystemExit(1) from None

    issues = validate_plan(plan)
    if not issues:
        click.secho(f"Plan '{plan.name}' is valid ({len(plan.units)} units)", fg="green")
        return

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in issues:
        color = "red" if issue.severity == "error" else "yellow"
        prefix = "ERROR" if issue.severity == "error" else "WARN"
        unit_str = f" [{issue.unit}]" if issue.unit else ""
        click.secho(f"  {prefix}{unit_str}: {issue.message}", fg=color)

    click.echo(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        raise SystemExit(1)


@plan_cmd.command("compile")
@click.argument("plan_file", type=click.Path(exists=True))
def plan_compile(plan_file):
    """Compile a plan into a DAG and show the tier view."""
    from dgov.dag_graph import compute_tiers, render_dry_run
    from dgov.plan import compile_plan, parse_plan_file, validate_plan

    try:
        plan = parse_plan_file(plan_file)
    except ValueError as e:
        click.secho(f"Parse error: {e}", fg="red")
        raise SystemExit(1) from None

    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        for issue in errors:
            unit_str = f" [{issue.unit}]" if issue.unit else ""
            click.secho(f"  ERROR{unit_str}: {issue.message}", fg="red")
        raise SystemExit(1)

    dag = compile_plan(plan)
    tiers = compute_tiers(dag.tasks)
    click.echo(render_dry_run(tiers, dag.tasks))
    click.echo(f"\nPlan '{plan.name}': {len(dag.tasks)} tasks, {len(tiers)} tiers")
    click.echo(f"Goal: {plan.goal}")


@plan_cmd.command("run")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("--max-concurrent", "-c", default=0, help="Max concurrent workers (0=unlimited)")
def plan_run(plan_file, max_concurrent):
    """Execute a plan through the DAG kernel."""
    from dgov.plan import run_plan

    try:
        result = run_plan(plan_file, max_concurrent=max_concurrent)
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    click.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status,
                "merged": result.merged,
                "failed": result.failed,
                "skipped": result.skipped,
            },
            indent=2,
        )
    )
