"""Merge queue commands for governor-side queue processing."""

from __future__ import annotations

import json
import os
import sys

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group("merge-queue")
def merge_queue():
    """Manage the merge request queue."""


@merge_queue.command("process")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option(
    "--resolve",
    type=click.Choice(["skip", "agent", "manual"]),
    default="skip",
    help="Conflict resolution strategy",
)
@click.option("--squash/--no-squash", default=True, help="Squash commits (default: on)")
@click.option("--rebase", is_flag=True, default=False, help="Rebase merge")
def process_merge(project_root, session_root, resolve, squash, rebase):
    """Claim and execute the next pending merge from the queue."""
    from dgov.persistence import claim_next_merge, complete_merge, emit_event

    session_root_abs = os.path.abspath(session_root or project_root)
    claimed = claim_next_merge(session_root_abs)
    if not claimed:
        click.echo(json.dumps({"status": "empty", "message": "No pending merges"}))
        return

    slug = claimed["branch"]  # branch field stores the pane slug
    ticket = claimed["ticket"]

    try:
        from dgov.merger import merge_worker_pane

        result = merge_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            resolve=resolve,
            squash=squash,
            rebase=rebase,
        )
        success = "error" not in result
        complete_merge(session_root_abs, ticket, success, json.dumps(result))
        emit_event(session_root_abs, "merge_completed", slug, ticket=ticket, success=success)
        click.echo(json.dumps({"ticket": ticket, "slug": slug, "result": result}))
        if not success:
            sys.exit(1)
    except Exception as exc:
        complete_merge(session_root_abs, ticket, False, json.dumps({"error": str(exc)}))
        click.echo(json.dumps({"ticket": ticket, "slug": slug, "error": str(exc)}), err=True)
        sys.exit(1)


@merge_queue.command("list")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option(
    "--status", "-s", default=None, help="Filter by status (pending, processing, done, failed)"
)
def list_queue(project_root, session_root, status):
    """List merge queue entries."""
    from dgov.persistence import list_merge_queue

    session_root_abs = os.path.abspath(session_root or project_root)
    entries = list_merge_queue(session_root_abs, status=status)
    click.echo(json.dumps(entries, indent=2, default=str))
