"""CLI commands for dgov plan execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click


@click.group("plan")
def plan_cmd():
    """Plan management: validate, compile, and run structured plans."""
    pass


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
@click.option("--dry-run", is_flag=True, default=True, help="Print tier view without executing")
def plan_compile(plan_file, dry_run):
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
    from dgov.dag import run_dag_via_kernel
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
    definition_hash = hashlib.sha256(Path(plan_file).read_bytes()).hexdigest()

    effective_concurrent = max_concurrent if max_concurrent > 0 else dag.max_concurrent

    click.echo(f"Running plan '{plan.name}' ({len(dag.tasks)} tasks)")
    result = run_dag_via_kernel(
        dag,
        dag_key=str(Path(plan_file).resolve()),
        definition_hash=definition_hash,
        auto_merge=True,
        max_concurrent=effective_concurrent,
    )

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
