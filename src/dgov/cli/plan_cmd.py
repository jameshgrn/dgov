"""CLI commands for dgov plan execution."""

from __future__ import annotations

import json
import os
from dataclasses import asdict

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
    """Create a scratch plan under .dgov/plans/.

    Examples:
      dgov plan scratch my-feature
    """
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
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def plan_validate(plan_file, output_json):
    """Validate a plan TOML file and print any issues.

    Examples:
      dgov plan validate .dgov/plans/my-plan.toml
    """
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

    if output_json:
        result = {
            "valid": len(errors) == 0,
            "plan": plan.name,
            "units": len(plan.units),
            "errors": [{"unit": i.unit, "message": i.message} for i in errors],
            "warnings": [{"unit": i.unit, "message": i.message} for i in warnings],
        }
        click.echo(json.dumps(result, indent=2))
        if errors:
            raise SystemExit(1)
        return

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
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def plan_compile(plan_file, output_json):
    """Compile a plan into a DAG and show the tier view.

    Examples:
      dgov plan compile .dgov/plans/my-plan.toml
    """
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

    if output_json:
        result = {
            "plan": plan.name,
            "goal": plan.goal,
            "tasks": len(dag.tasks),
            "tiers": len(tiers),
            "dag": asdict(dag),
        }
        click.echo(json.dumps(result, indent=2, default=str))
        return

    click.echo(render_dry_run(tiers, dag.tasks))
    click.echo(f"\nPlan '{plan.name}': {len(dag.tasks)} tasks, {len(tiers)} tiers")
    click.echo(f"Goal: {plan.goal}")


@plan_cmd.command("run")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("--max-concurrent", "-c", default=0, help="Max concurrent workers (0=unlimited)")
@click.option("--wait", is_flag=True, help="Block until DAG completes (pipe-driven, no polling)")
def plan_run(plan_file, max_concurrent, wait):
    """Execute a plan through the DAG kernel.

    Examples:
      dgov plan run .dgov/plans/my-plan.toml
      dgov plan run .dgov/plans/my-plan.toml --wait
    """
    from dgov.plan import check_cross_plan_claims, parse_plan_file, run_plan

    try:
        plan = parse_plan_file(plan_file)
    except ValueError:
        plan = None

    if plan:
        session_root = os.path.abspath(".")
        claim_warnings = check_cross_plan_claims(plan, session_root)
        for w in claim_warnings:
            click.secho(f"  WARN: {w.message}", fg="yellow")

    try:
        result = run_plan(plan_file, max_concurrent=max_concurrent)
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    run_id = result.run_id
    click.echo(json.dumps({"run_id": run_id, "status": result.status}, indent=2))

    if not wait:
        return

    _wait_for_dag(run_id)


def _scaffold_auto(goal: str, files: list[str], name: str) -> str:
    """Generate a complete plan TOML using PlanGenerationProvider."""
    from pathlib import Path

    from dgov.decision import DecisionKind, GeneratePlanRequest
    from dgov.provider_registry import get_provider

    file_contents: list[tuple[str, str]] = []
    for f in files:
        try:
            file_contents.append((f, Path(f).read_text()[:10000]))
        except OSError:
            pass

    plan_examples: list[str] = []
    plans_dir = Path(".dgov/plans")
    if plans_dir.is_dir():
        for plan_file in sorted(plans_dir.glob("*.toml"))[:2]:
            try:
                plan_examples.append(plan_file.read_text()[:3000])
            except OSError:
                pass

    active_claims: list[str] = []
    try:
        from dgov.persistence import list_active_dag_runs

        session_root = os.path.abspath(".")
        for run in list_active_dag_runs(session_root):
            try:
                import json

                defn = json.loads(run.get("definition_json", "{}"))
                for task in defn.get("tasks", []):
                    active_claims.extend(task.get("file_claims", []))
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass

    request = GeneratePlanRequest(
        goal=goal,
        files=tuple(files),
        file_contents=tuple(file_contents),
        plan_examples=tuple(plan_examples),
        active_claims=tuple(sorted(set(active_claims))),
    )

    provider = get_provider(DecisionKind.GENERATE_PLAN, session_root=os.path.abspath("."))
    result = provider.generate_plan(request)
    decision = result.decision

    if decision.questions:
        click.secho("Planner has questions:", fg="yellow")
        for q in decision.questions:
            click.secho(f"  ? {q}", fg="yellow")
        click.echo()

    if not decision.valid:
        click.secho("Warning: generated plan has validation issues:", fg="yellow")
        for issue in decision.validation_issues:
            click.secho(f"  - {issue}", fg="yellow")
        click.echo()

    return decision.plan_toml


@plan_cmd.command("scaffold")
@click.option("--goal", required=True, help="Plan goal statement")
@click.option("--files", required=True, multiple=True, help="Files to edit (repeat for multiple)")
@click.option("--name", default="", help="Plan name (derived from goal if empty)")
@click.option("-o", "--output", default="", help="Write to file instead of stdout")
@click.option("--dry-run", is_flag=True, help="Print to stdout even if -o is set")
@click.option("--auto", is_flag=True, help="Use LLM to generate complete plan (not just template)")
@click.option("--run", is_flag=True, help="With --auto: validate and execute the generated plan")
@click.option("--wait", "wait_for_run", is_flag=True, help="With --run: block until DAG completes")
def plan_scaffold(goal, files, name, output, dry_run, auto, run, wait_for_run):
    """Generate a TOML plan template from goal and file list.

    Examples:
      dgov plan scaffold --goal "Add logging" --files src/dgov/cli.py
      dgov plan scaffold --goal "Fix parser" --files src/dgov/parser.py --auto --run --wait
    """
    if run and not auto:
        click.secho("--run requires --auto", fg="red")
        raise SystemExit(1)

    if auto:
        toml_text = _scaffold_auto(goal, list(files), name)
    else:
        from dgov.plan import scaffold_plan

        toml_text = scaffold_plan(goal, list(files), name=name)

    if run:
        _scaffold_and_run(toml_text, output, wait_for_run)
        return

    if output and not dry_run:
        with open(output, "w") as f:
            f.write(toml_text)
        click.echo(f"Wrote {output}")
    else:
        click.echo(toml_text)


def _scaffold_and_run(toml_text: str, output: str, wait: bool) -> None:
    """Write generated plan to tempfile, validate, and execute."""
    import tempfile

    from dgov.plan import parse_plan_file, run_plan, validate_plan

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", prefix="dgov-plan-", delete=False
    ) as f:
        f.write(toml_text)
        tmp_path = f.name

    if output:
        with open(output, "w") as f:
            f.write(toml_text)
        click.echo(f"Wrote {output}")

    try:
        plan = parse_plan_file(tmp_path)
    except ValueError as e:
        click.secho(f"Generated plan failed to parse: {e}", fg="red")
        raise SystemExit(1) from None

    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        for issue in errors:
            unit_str = f" [{issue.unit}]" if issue.unit else ""
            click.secho(f"  ERROR{unit_str}: {issue.message}", fg="red")
        click.secho(f"Plan saved to {tmp_path}", fg="yellow")
        raise SystemExit(1)

    for issue in issues:
        if issue.severity == "warning":
            click.secho(f"  WARN: {issue.message}", fg="yellow")

    click.secho(f"Plan valid ({len(plan.units)} units). Executing...", fg="green")

    try:
        result = run_plan(tmp_path)
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    run_id = result.run_id
    click.echo(json.dumps({"run_id": run_id, "status": result.status, "plan_file": tmp_path}))

    if not wait:
        return

    _wait_for_dag(run_id)


def _wait_for_dag(run_id: int) -> None:
    """Block on DAG events until evals_verified."""
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
            dag_rid = ev.get("dag_run_id")
            if dag_rid is not None and dag_rid != run_id:
                continue
            if kind == "dag_task_dispatched":
                click.echo(f"  dispatched: {ev.get('task', ev.get('pane', ''))}")
            elif kind == "pane_done":
                click.echo(f"  done: {ev.get('pane', '')}")
            elif kind == "pane_failed":
                click.echo(f"  failed: {ev.get('pane', '')}")
            elif kind == "review_pass":
                click.echo(f"  review pass: {ev.get('pane', '')}")
            elif kind == "review_fail":
                reason = ev.get("reason", "")
                reason_str = f" — {reason}" if reason else ""
                click.echo(f"  review fail: {ev.get('pane', '')}{reason_str}")
            elif kind == "merge_completed":
                click.echo(f"  merged: {ev.get('pane', '')}")
            elif kind in ("dag_completed", "dag_failed"):
                click.echo(f"  DAG {kind.split('_')[1]}")
            elif kind == "evals_verified":
                run = get_dag_run(session_root, run_id)
                status = run["status"] if run else "unknown"
                eval_results = run.get("eval_results", []) if run else []
                passed = sum(1 for r in eval_results if r["passed"])
                failed_count = sum(1 for r in eval_results if not r["passed"])
                click.echo(f"\nDAG run {run_id}: {status}")
                for r in eval_results:
                    m = "PASS" if r["passed"] else "FAIL"
                    c = "green" if r["passed"] else "red"
                    click.secho(f"  [{m}] {r['eval_id']}", fg=c)
                    if not r["passed"] and r.get("output"):
                        output_preview = r["output"][:200].replace("\n", " ").strip()
                        if output_preview:
                            click.secho(f"         {output_preview}", fg="yellow")
                if eval_results:
                    click.echo(f"  {passed} passed, {failed_count} failed")
                if status != "completed":
                    raise SystemExit(1)
                if failed_count:
                    raise SystemExit(2)
                return


@plan_cmd.command("verify")
@click.argument("run_id", type=int)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--timeout", default=60, help="Timeout per evidence command (seconds)")
def plan_verify(run_id, project_root, session_root, timeout):
    """Run eval evidence commands for a DAG run and report pass/fail.

    Examples:
      dgov plan verify 42 -r .
    """
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
            # Show first 200 chars of failure output, indented
            output_preview = r["output"][:200].replace("\n", " ").strip()
            if output_preview:
                click.secho(f"         {output_preview}", fg="yellow")

    click.echo(f"\n{passed} passed, {failed} failed, {len(results)} total")
    if failed:
        raise SystemExit(2)


@plan_cmd.command("resume")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option(
    "--run-id", type=int, default=None, help="Specific run ID (default: most recent failed)"
)
@click.option("--wait", is_flag=True, help="Block until DAG completes")
@click.option("--max-concurrent", "-c", default=0, help="Max concurrent workers (0=unlimited)")
def plan_resume(plan_file, run_id, wait, max_concurrent):
    """Resume a failed or partial plan run, skipping already-merged units.

    Examples:
      dgov plan resume .dgov/plans/my-plan.toml --wait
    """
    from pathlib import Path

    from dgov.persistence import (
        _get_db,
        ensure_dag_tables,
        get_dag_run,
        list_dag_tasks,
    )

    abs_path = str(Path(plan_file).resolve())
    session_root = os.path.abspath(".")
    ensure_dag_tables(session_root)

    if run_id is not None:
        existing = get_dag_run(session_root, run_id)
        if not existing:
            click.secho(f"DAG run {run_id} not found", fg="red")
            raise SystemExit(1)
        if existing["status"] not in ("failed", "partial"):
            click.secho(
                f"Run {run_id} status is '{existing['status']}' — only failed/partial can resume",
                fg="red",
            )
            raise SystemExit(1)
    else:
        conn = _get_db(session_root)
        row = conn.execute(
            "SELECT id, status FROM dag_runs"
            " WHERE dag_file = ? AND status IN (?, ?)"
            " ORDER BY id DESC LIMIT 1",
            (abs_path, "failed", "partial"),
        ).fetchone()
        if not row:
            click.secho(f"No failed or partial runs found for {plan_file}", fg="red")
            raise SystemExit(1)
        run_id = row[0]
        click.echo(f"Resuming run {run_id} (status: {row[1]})")

    task_rows = list_dag_tasks(session_root, run_id)
    already_done = {r["slug"] for r in task_rows if r.get("status") in ("merged",)}
    if already_done:
        click.echo(f"Skipping {len(already_done)} merged: {', '.join(sorted(already_done))}")

    from dgov.executor import run_resume_dag

    run_resume_dag(session_root, run_id)

    from dgov.plan import run_plan

    try:
        result = run_plan(plan_file, max_concurrent=max_concurrent, skip=already_done or None)
    except ValueError as e:
        click.secho(str(e), fg="red")
        raise SystemExit(1) from None

    new_run_id = result.run_id
    click.echo(json.dumps({"run_id": new_run_id, "resumed_from": run_id, "status": result.status}))

    if not wait:
        return

    _wait_for_dag(new_run_id)
