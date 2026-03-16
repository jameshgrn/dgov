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
@click.option("--dry-run", is_flag=True, help="Parse and print execution plan without running")
@click.option("--tier", type=int, default=None, help="Run only tiers 0..N (inclusive, zero-based)")
@click.option("--skip", multiple=True, help="Skip a task slug (repeatable)")
@click.option("--max-retries", type=int, default=1, help="Max retries per task before escalation")
@click.option("--auto-merge/--no-auto-merge", default=True, help="Auto-merge reviewed-pass tasks")
def dag_run(dagfile, dry_run, tier, skip, max_retries, auto_merge):
    """Execute a TOML DAG file."""
    from dgov.dag import compute_tiers, parse_dag_file, render_dry_run, run_dag

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
        max_retries=max_retries,
        auto_merge=auto_merge,
    )
    click.echo(json.dumps(asdict(summary), indent=2, default=str))
    if summary.failed:
        raise SystemExit(1)


@dag.command("merge")
@click.argument("dagfile", type=click.Path(exists=True))
def dag_merge(dagfile):
    """Merge an awaiting_merge DAG run in topological order."""
    from dgov.dag import merge_dag

    summary = merge_dag(dagfile)
    click.echo(json.dumps(asdict(summary), indent=2, default=str))
    if summary.failed:
        raise SystemExit(1)
