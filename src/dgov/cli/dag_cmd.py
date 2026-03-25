"""CLI commands for DAG execution."""

from __future__ import annotations

import json
from dataclasses import asdict

import click


@click.group("dag")
def dag():
    """DAG execution commands."""


@dag.command("run")
@click.argument("dagfile", type=click.Path(exists=True))
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@click.option("--dry-run", is_flag=True, help="Parse and print execution plan without running")
@click.option("--tier", type=int, default=None, help="Run only tiers 0..N (inclusive, zero-based)")
@click.option("--skip", multiple=True, help="Skip a task slug (repeatable)")
@click.option("--auto-merge/--no-auto-merge", default=True, help="Auto-merge reviewed-pass tasks")
@click.option(
    "--max-concurrent",
    type=int,
    default=0,
    help="Max tasks dispatched simultaneously per tier (0=unlimited)",
)
def dag_run(dagfile, project_root, dry_run, tier, skip, auto_merge, max_concurrent):
    """Execute a TOML DAG file."""
    from dgov.dag import compute_tiers, parse_dag_file, render_dry_run, run_dag

    try:
        if dry_run:
            dag_def = parse_dag_file(dagfile)
            tiers = compute_tiers(dag_def.tasks)
            click.echo(render_dry_run(tiers, dag_def.tasks))
            return

        summary = run_dag(
            dagfile,
            dry_run=False,
            tier_limit=tier,
            skip=set(skip) if skip else None,
            auto_merge=auto_merge,
            max_concurrent=max_concurrent,
        )
        click.echo(json.dumps(asdict(summary), indent=2, default=str))
        if summary.failed:
            raise SystemExit(1)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@dag.command("merge")
@click.argument("dagfile", type=click.Path(exists=True))
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
def dag_merge(dagfile, project_root):
    """Merge an awaiting_merge DAG run in topological order."""
    from dgov.dag import merge_dag

    try:
        summary = merge_dag(dagfile)
        click.echo(json.dumps(asdict(summary), indent=2, default=str))
        if summary.failed:
            raise SystemExit(1)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@dag.command("resume")
@click.argument("dagfile", type=click.Path(exists=True))
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@click.option(
    "--run-id",
    type=int,
    default=None,
    help="Specific run ID to resume (default: most recent failed)",
)
@click.option("--max-concurrent", type=int, default=0, help="Max tasks per tier (0=unlimited)")
def dag_resume(dagfile, project_root, run_id, max_concurrent):
    """Resume a failed DAG run, re-executing unmerged tasks."""
    import os
    from pathlib import Path

    from dgov.cli.pane import _autocorrect_roots
    from dgov.persistence import (
        ensure_dag_tables,
        get_dag_run,
    )

    abs_path = str(Path(dagfile).resolve())
    project_root, session_root = _autocorrect_roots(project_root, None)
    session_root = os.path.abspath(session_root or project_root)

    try:
        ensure_dag_tables(session_root)

        if run_id is not None:
            existing = get_dag_run(session_root, run_id)
            if not existing:
                raise click.ClickException(f"DAG run {run_id} not found")
            if existing["status"] not in ("failed", "partial"):
                raise click.ClickException(
                    f"Run {run_id} has status '{existing['status']}' "
                    "-- only failed/partial runs can be resumed"
                )
            if existing["dag_file"] != abs_path:
                raise click.ClickException(
                    f"Run {run_id} was for {existing['dag_file']}, not {abs_path}"
                )
        else:
            # Find most recent failed/partial run for this DAG file
            from dgov.persistence import _get_db

            conn = _get_db(session_root)
            row = conn.execute(
                "SELECT id, status FROM dag_runs"
                " WHERE dag_file = ? AND status IN (?, ?)"
                " ORDER BY id DESC LIMIT 1",
                (abs_path, "failed", "partial"),
            ).fetchone()
            if not row:
                raise click.ClickException(f"No failed or partial runs found for {abs_path}")
            run_id = row[0]
            click.echo(f"Resuming run {run_id} (status: {row[1]})")

        # Collect already-merged tasks so we can skip them in the new run
        from dgov.persistence import list_dag_tasks

        task_rows = list_dag_tasks(session_root, run_id)
        already_done = {r["slug"] for r in task_rows if r.get("status") in ("merged",)}
        click.echo(f"Skipping {len(already_done)} already-merged tasks: {sorted(already_done)}")

        from dgov.executor import run_resume_dag

        run_resume_dag(session_root, run_id)

        from dgov.dag import run_dag

        summary = run_dag(
            dagfile,
            dry_run=False,
            skip=already_done or None,
            auto_merge=True,
            max_concurrent=max_concurrent,
        )
        click.echo(json.dumps(asdict(summary), indent=2, default=str))
        if summary.failed:
            raise SystemExit(1)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@dag.command("status")
@click.argument("dagfile", type=click.Path(exists=True))
@click.option("--run-id", type=int, default=None, help="Specific run ID (default: most recent)")
def dag_status(dagfile, run_id):
    """Show status of a DAG run: tasks, agents, states, and eval contract."""
    import os
    from pathlib import Path

    from dgov.persistence import (
        _get_db,
        ensure_dag_tables,
        get_dag_run,
        list_dag_tasks,
    )

    abs_path = str(Path(dagfile).resolve())
    session_root = os.path.abspath(".")
    ensure_dag_tables(session_root)

    if run_id is not None:
        run = get_dag_run(session_root, run_id)
        if not run:
            raise click.ClickException(f"DAG run {run_id} not found")
    else:
        conn = _get_db(session_root)
        row = conn.execute(
            "SELECT id FROM dag_runs WHERE dag_file = ? ORDER BY id DESC LIMIT 1",
            (abs_path,),
        ).fetchone()
        if not row:
            raise click.ClickException(f"No runs found for {abs_path}")
        run = get_dag_run(session_root, row[0])
        if not run:
            raise click.ClickException(f"DAG run {row[0]} not found")

    run_id = run["id"]
    tasks = list_dag_tasks(session_root, run_id)

    click.echo(f"DAG run {run_id}: {run['status']}")
    click.echo(f"  file: {run['dag_file']}")
    click.echo(f"  started: {run.get('started_at', 'unknown')}")
    click.echo(f"  tier: {run.get('current_tier', '?')}")

    # Eval contract
    evals = run.get("evals", [])
    links = run.get("unit_eval_links", [])
    eval_results = {r["eval_id"]: r for r in run.get("eval_results", [])}
    if evals:
        click.echo()
        click.echo("Evals:")
        # Build unit→eval and eval→units maps from links
        eval_units: dict[str, list[str]] = {}
        for link in links:
            eval_units.setdefault(link["eval_id"], []).append(link["unit_slug"])

        # Build task status lookup
        task_status = {t["slug"]: t.get("status", "pending") for t in tasks}

        for ev in evals:
            eid = ev["eval_id"]
            kind = ev["kind"]
            stmt = ev["statement"]
            satisfying = eval_units.get(eid, [])
            # Use evidence result if available, otherwise derive from unit status
            er = eval_results.get(eid)
            if er is not None:
                marker = "PASS" if er["passed"] else "FAIL"
            elif not satisfying:
                marker = "?"
            elif all(task_status.get(u) == "merged" for u in satisfying):
                marker = "PASS"
            elif any(task_status.get(u) in ("failed", "abandoned") for u in satisfying):
                marker = "FAIL"
            else:
                marker = "..."
            units_str = ", ".join(satisfying) if satisfying else "(none)"
            click.echo(f"  [{marker:4s}] {eid} ({kind}): {stmt}")
            click.echo(f"         units: {units_str}")
            if er is not None and not er["passed"] and er.get("output"):
                click.echo(f"         evidence: {er['output'][:80]}")

    click.echo()

    if not tasks:
        click.echo("  (no tasks)")
        return

    # Build unit→evals map for display
    unit_evals: dict[str, list[str]] = {}
    for link in links:
        unit_evals.setdefault(link["unit_slug"], []).append(link["eval_id"])

    # Column widths
    max_slug = max(len(t["slug"]) for t in tasks)
    max_agent = max(len(t.get("agent", "?")) for t in tasks)

    for t in tasks:
        slug_str = t["slug"].ljust(max_slug)
        agent = t.get("agent", "?").ljust(max_agent)
        status = t.get("status", "?")
        attempt = t.get("attempt", 1)
        error = t.get("error", "")
        line = f"  {slug_str}  {agent}  {status}"
        if attempt and attempt > 1:
            line += f"  (attempt {attempt})"
        if error:
            line += f"  [{error[:60]}]"
        satisfies = unit_evals.get(t["slug"], [])
        if satisfies:
            line += f"  satisfies: {', '.join(satisfies)}"
        click.echo(line)
