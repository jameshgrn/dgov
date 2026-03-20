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
@click.option("--timeout", default=120, type=int, help="Timeout in seconds (default: 120)")
def process_merge(project_root, session_root, resolve, squash, rebase, timeout):
    """Claim and execute the next pending merge from the queue."""
    import signal

    from dgov.persistence import claim_next_merge, complete_merge, emit_event

    session_root_abs = os.path.abspath(session_root or project_root)
    claimed = claim_next_merge(session_root_abs)
    if not claimed:
        click.echo(json.dumps({"status": "empty", "message": "No pending merges"}))
        return

    slug = claimed["branch"]  # branch field stores the pane slug
    ticket = claimed["ticket"]

    def _timeout_handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"merge-queue process timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)

    try:
        from dgov.executor import run_land_only

        landed = run_land_only(
            project_root,
            slug,
            session_root=session_root,
            resolve=resolve,
            squash=squash,
            rebase=rebase,
        )
        result = landed.merge_result or {"error": landed.error or "Review failed"}
        success = "error" not in result
        complete_merge(session_root_abs, ticket, success, json.dumps(result))
        if success:
            emit_event(session_root_abs, "merge_completed", slug, ticket=ticket, success=True)
        click.echo(json.dumps({"ticket": ticket, "slug": slug, "result": result}))
        if not success:
            sys.exit(1)
    except TimeoutError as exc:
        complete_merge(session_root_abs, ticket, False, json.dumps({"error": str(exc)}))
        click.echo(json.dumps({"ticket": ticket, "slug": slug, "error": str(exc)}), err=True)
        sys.exit(1)
    except Exception as exc:
        complete_merge(session_root_abs, ticket, False, json.dumps({"error": str(exc)}))
        click.echo(json.dumps({"ticket": ticket, "slug": slug, "error": str(exc)}), err=True)
        sys.exit(1)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


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
