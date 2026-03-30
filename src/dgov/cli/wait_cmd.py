"""CLI command for event-driven waiting and governor interrupts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION


def _get_ev_field(ev: dict, field: str) -> object:
    """Read field from flattened event, with legacy fallback to JSON data blob."""
    # Try top-level flattened field first
    if field in ev:
        return ev[field]
    # Legacy fallback: parse JSON from 'data' string
    data = ev.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data).get(field)
        except json.JSONDecodeError:
            pass
    return None


@click.command("wait")
@click.option("--interrupts", is_flag=True, help="Block on governor interrupts and show payload.")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def wait_cmd(interrupts, project_root, session_root):
    """Wait for DAG completion or governor interrupts."""
    from dgov.persistence import latest_event_id, wait_for_events

    session_root = os.path.abspath(session_root or project_root)
    cursor = latest_event_id(session_root)

    if interrupts:
        click.echo(
            "Waiting for governor interrupts or DAG completion (per-process notify pipe)..."
        )
    else:
        click.echo("Waiting for DAG completion...")

    while True:
        events = wait_for_events(
            session_root,
            after_id=cursor,
            event_types=("dag_blocked", "dag_cancelled", "dag_completed", "dag_failed"),
            timeout_s=60.0,
        )
        for ev in events:
            cursor = max(cursor, ev["id"])
            kind = ev["event"]

            if kind == "dag_blocked":
                # Read from flattened event contract (top-level fields)
                task = _get_ev_field(ev, "task")
                reason = _get_ev_field(ev, "reason")
                report_path_val = _get_ev_field(ev, "report_path")
                report_path = report_path_val if isinstance(report_path_val, str) else None
                click.secho(f"\n[INTERRUPT] Task {task} is blocked!", fg="red", bold=True)
                click.echo(f"Reason: {reason}")

                if report_path and Path(report_path).is_file():
                    report = json.loads(Path(report_path).read_text())
                    click.secho("\n--- ROLE ---", fg="yellow")
                    click.echo(report.get("role"))

                    click.secho("\n--- LOG TAIL ---", fg="yellow")
                    click.echo(report.get("log_tail"))

                    click.secho("\n--- DIFF ---", fg="yellow")
                    click.echo(report.get("diff"))

                    click.secho(f"\nReport: {report_path}", dim=True)

                if interrupts:
                    # Return to governor context
                    return

            if kind == "dag_cancelled":
                dag_run_id = _get_ev_field(ev, "dag_run_id")
                click.secho(f"\nDAG CANCELLED: Run {dag_run_id}", fg="yellow", bold=True)
                return

            if kind in ("dag_completed", "dag_failed"):
                dag_run_id = _get_ev_field(ev, "dag_run_id")
                status_val = _get_ev_field(ev, "status")
                # Narrow to str: use event kind as fallback when status is missing or non-string
                status = status_val if isinstance(status_val, str) else kind.split("_")[1]
                color = "green" if status == "completed" else "red"
                click.secho(f"\nDAG {status.upper()}: Run {dag_run_id}", fg=color, bold=True)
                return
