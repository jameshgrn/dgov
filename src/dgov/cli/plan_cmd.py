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
@click.option("--wait", is_flag=True, help="Block until DAG completes (pipe-driven, no polling)")
def plan_run(plan_file, max_concurrent, wait):
    """Execute a plan through the DAG kernel."""
    from dgov.plan import run_plan

    try:
        result = run_plan(plan_file, max_concurrent=max_concurrent)
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    run_id = result.run_id
    click.echo(json.dumps({"run_id": run_id, "status": result.status}, indent=2))

    if not wait:
        return

    # Stream progress events, then block on evals_verified for the terminal signal.
    from dgov.persistence import get_dag_run, latest_event_id, wait_for_events

    session_root = os.path.abspath(".")
    cursor = latest_event_id(session_root)

    _PROGRESS_EVENTS = (
        "dag_task_dispatched",
        "pane_done",
        "pane_failed",
        "review_pass",
        "review_fail",
        "merge_completed",
        "dag_completed",
        "dag_failed",
        "evals_verified",
    )

    while True:
        events = wait_for_events(
            session_root,
            after_id=cursor,
            event_types=_PROGRESS_EVENTS,
            timeout_s=3600.0,
        )
        for ev in events:
            cursor = max(cursor, ev["id"])
            kind = ev.get("event", "")

            # Print progress for events belonging to our run
            dag_rid = ev.get("dag_run_id")
            if dag_rid is not None and dag_rid != run_id:
                continue

            if kind == "dag_task_dispatched":
                task = ev.get("task", ev.get("pane", ""))
                click.echo(f"  dispatched: {task}")
            elif kind == "pane_done":
                click.echo(f"  done: {ev.get('pane', '')}")
            elif kind == "pane_failed":
                click.echo(f"  failed: {ev.get('pane', '')}")
            elif kind == "review_pass":
                click.echo(f"  review pass: {ev.get('pane', '')}")
            elif kind == "review_fail":
                click.echo(f"  review fail: {ev.get('pane', '')}")
            elif kind == "merge_completed":
                click.echo(f"  merged: {ev.get('pane', '')}")
            elif kind in ("dag_completed", "dag_failed"):
                click.echo(f"  DAG {kind.split('_')[1]}")
            elif kind == "evals_verified":
                run = get_dag_run(session_root, run_id)
                status = run["status"] if run else "unknown"
                eval_results = run.get("eval_results", []) if run else []
                passed = sum(1 for r in eval_results if r["passed"])
                failed = sum(1 for r in eval_results if not r["passed"])

                click.echo(f"\nDAG run {run_id}: {status}")
                for r in eval_results:
                    m = "PASS" if r["passed"] else "FAIL"
                    c = "green" if r["passed"] else "red"
                    click.secho(f"  [{m}] {r['eval_id']}", fg=c)
                if eval_results:
                    click.echo(f"  {passed} passed, {failed} failed")
                if status != "completed" or failed:
                    raise SystemExit(1)
                return


@plan_cmd.command("verify")
@click.argument("run_id", type=int)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--timeout", default=60, help="Timeout per evidence command (seconds)")
def plan_verify(run_id, project_root, session_root, timeout):
    """Run eval evidence commands for a DAG run and report pass/fail."""
    from dgov.plan import verify_eval_evidence

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root

    results = verify_eval_evidence(
        session_root, run_id, project_root=project_root, timeout_s=timeout
    )

    if not results:
        click.echo("No evals with evidence commands found.")
        return

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])

    for r in results:
        marker = "PASS" if r["passed"] else "FAIL"
        color = "green" if r["passed"] else "red"
        click.secho(f"  [{marker}] {r['eval_id']} ({r['kind']}): {r['statement']}", fg=color)
        if not r["passed"] and r["output"]:
            click.echo(f"         {r['output'][:100]}")

    click.echo(f"\n{passed} passed, {failed} failed, {len(results)} total")
    if failed:
        raise SystemExit(1)
