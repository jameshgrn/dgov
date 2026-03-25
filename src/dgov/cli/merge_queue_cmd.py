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

    def _timeout_handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"merge-queue process timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)

    try:
        from dgov.executor import run_process_merge

        result = run_process_merge(
            project_root,
            session_root or "",
            resolve=resolve,
            squash=squash,
            rebase=rebase,
        )
        click.echo(json.dumps(result, indent=2))
        if result.get("status") == "empty":
            return
        if not result.get("success", False):
            sys.exit(1)
    except TimeoutError as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
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
