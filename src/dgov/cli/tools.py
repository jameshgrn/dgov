"""Tool telemetry audit CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.persistence import read_events
from dgov.project_root import resolve_project_root
from dgov.tool_audit import ToolAuditSummary, summarize_tool_events


@cli.group(name="tools")
def tools_cmd() -> None:
    """Tool telemetry and audit commands."""
    pass


@tools_cmd.command(name="audit")
@click.option("--plan", "plan_name", help="Only include events from this plan.")
@click.option("--role", help="Only include events for this worker role.")
@click.option("--limit", default=0, type=int, help="Limit rendered tool rows. 0 means all.")
@click.option("--root", "-r", default=".", help="Project root")
def tools_audit(plan_name: str | None, role: str | None, limit: int, root: str) -> None:
    """Summarize worker tool-call telemetry from the event log."""
    project_root = resolve_project_root(Path(root))
    events = read_events(str(project_root), plan_name=plan_name)
    summary = summarize_tool_events(events, plan_name=plan_name, role=role)
    if want_json():
        click.echo(json.dumps(summary.as_dict(limit=limit), indent=2))
        return
    _render_human(summary, limit=limit)


def _render_human(summary: ToolAuditSummary, *, limit: int = 0) -> None:
    if summary.total_calls == 0:
        click.echo("No worker tool-call events found.")
        return

    click.echo("Tool audit")
    if summary.plan_name:
        click.echo(f"plan: {summary.plan_name}")
    if summary.role:
        click.echo(f"role: {summary.role}")
    click.echo(f"calls: {summary.total_calls}")
    click.echo(f"failures: {summary.total_failures}")
    click.echo(f"clipped results: {summary.total_clipped_results}")
    click.echo("")
    click.echo(
        f"{'tool':24s} {'calls':>5s} {'fail%':>6s} {'clip':>5s} "
        f"{'avg_ms':>8s} {'avg_chars':>9s} {'roles':18s} top_error"
    )
    click.echo("-" * 95)
    rows = summary.rows[:limit] if limit > 0 else summary.rows
    for row in rows:
        roles = ",".join(row.roles) if row.roles else "-"
        top_error = row.top_error_kind or "-"
        click.echo(
            f"{row.tool:24.24s} {row.calls:5d} {row.failure_rate * 100:5.1f}% "
            f"{row.clipped_results:5d} {row.average_duration_ms:8.1f} "
            f"{row.average_result_chars:9.1f} {roles:18.18s} {top_error}"
        )
