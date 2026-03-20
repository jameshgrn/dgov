"""Decision journal query CLI."""

from __future__ import annotations

import json

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.command("journal")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option("--kind", "-k", default=None, help="Filter by decision kind")
@click.option("--pane", "-p", default=None, help="Filter by pane slug")
@click.option("--limit", "-n", default=20, type=int, help="Max entries to show (default 20)")
@click.option("--json-output", "--json", is_flag=True, help="Raw JSON output")
def journal_cmd(project_root, session_root, kind, pane, limit, json_output):
    """Query the decision journal."""
    import os

    from dgov.persistence import read_decision_journal

    session_root = os.path.abspath(session_root or project_root)
    rows = read_decision_journal(session_root, kind=kind, pane_slug=pane, limit=limit)
    if json_output:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo("No journal entries found.")
        return
    for row in rows:
        ts = row.get("ts", "")[:19]
        k = row.get("kind", "?")
        provider = row.get("provider_id", "?")
        model = row.get("model_id") or "-"
        slug = row.get("pane_slug") or "-"
        dur = row.get("duration_ms")
        dur_str = f"{dur:.0f}ms" if dur else "-"
        err = row.get("error")
        status = "ERR" if err else "OK"
        click.echo(
            f"{ts}  {k:<16s} {provider:<24s} {model:<16s} {slug:<20s} {dur_str:>8s}  {status}"
        )
