"""Operational ledger CLI."""

from __future__ import annotations

import json
import os

import click

from dgov.cli import SESSION_ROOT_OPTION, want_json


@click.group("ledger")
def ledger_cmd():
    """Query and manage the operational ledger."""


@ledger_cmd.command("add")
@click.argument("category")
@click.argument("summary")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option("--detail", "-d", default="", help="Extended detail")
@click.option(
    "--severity",
    "-s",
    default="info",
    type=click.Choice(["info", "low", "medium", "high"]),
)
@click.option(
    "--status",
    default="open",
    type=click.Choice(["open", "fixed", "accepted", "wontfix"]),
)
@click.option("--tag", "-t", multiple=True, help="Tags (repeatable)")
@click.option("--slug", multiple=True, help="Linked pane slugs (repeatable)")
def ledger_add_cmd(
    project_root, session_root, category, summary, detail, severity, status, tag, slug
):
    """Add a ledger entry. Categories: bug, fix, rule, pattern, debt, capability, decision.

    Examples:
      dgov ledger add bug "Parser fails on empty input" -r . -s medium -t parser
      dgov ledger add rule "Always run preflight" -r . --status accepted
    """
    from dgov.spans import ledger_add

    session_root = os.path.abspath(session_root or project_root)
    entry_id = ledger_add(
        session_root,
        category,
        summary,
        detail=detail,
        severity=severity,
        status=status,
        linked_slugs=list(slug),
        tags=list(tag),
    )
    click.echo(json.dumps({"id": entry_id, "category": category, "summary": summary}))


@ledger_cmd.command("list")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option(
    "--status",
    "-s",
    default=None,
    type=click.Choice(["open", "fixed", "accepted", "wontfix"]),
)
@click.option("--tag", "-t", default=None, help="Filter by tag")
@click.option("--limit", "-n", default=20, type=int)
@click.option("--json-output", "--json", is_flag=True, help="Raw JSON")
def ledger_list_cmd(project_root, session_root, category, status, tag, limit, json_output):
    """List ledger entries.

    Examples:
      dgov ledger list -r . -c bug -s open
      dgov ledger list -r . -c rule --json
    """
    from dgov.spans import ledger_query

    session_root = os.path.abspath(session_root or project_root)
    entries = ledger_query(session_root, category=category, status=status, tag=tag, limit=limit)
    if json_output or want_json():
        click.echo(json.dumps(entries, indent=2, default=str))
        return
    if not entries:
        click.echo("No ledger entries found.")
        return
    for e in entries:
        ts = e["ts"][:16]
        cat = e["category"]
        sev = e["severity"]
        st = e["status"]
        summary = e["summary"][:80]
        eid = e["id"]
        click.echo(f"  #{eid:<4d} {ts}  {cat:<12s} {sev:<6s} {st:<8s} {summary}")


@ledger_cmd.command("resolve")
@click.argument("entry_id", type=int)
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option(
    "--status",
    "-s",
    default="fixed",
    type=click.Choice(["fixed", "accepted", "wontfix"]),
)
def ledger_resolve_cmd(entry_id, project_root, session_root, status):
    """Resolve a ledger entry.

    Examples:
      dgov ledger resolve 42 -r .
      dgov ledger resolve 42 -s wontfix
    """
    from dgov.spans import ledger_query, ledger_update

    session_root = os.path.abspath(session_root or project_root)

    # Idempotent: already in target status → no-op
    existing = ledger_query(session_root, limit=1)
    for e in existing:
        if e["id"] == entry_id and e["status"] == status:
            click.echo(json.dumps({"already": status, "id": entry_id}))
            return

    ledger_update(session_root, entry_id, status=status)
    click.echo(json.dumps({"resolved": status, "id": entry_id}))
