"""CLI command: dgov briefing — on-demand document viewer via glow."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION


def _reports_dir(session_root: str) -> Path:
    return Path(session_root) / ".dgov" / "reports"


@click.group("briefing", invoke_without_command=True)
@click.argument("name", required=False)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.pass_context
def briefing_cmd(ctx, name: str | None, project_root: str, session_root: str | None) -> None:
    """View reports and documents with glow.

    \b
    dgov briefing                  # list available reports
    dgov briefing cursor-arch      # open a report in glow pane
    dgov briefing /path/to/doc.md  # open any markdown file
    """
    if ctx.invoked_subcommand is not None:
        return

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    if name is None:
        # List available reports
        reports = _reports_dir(session_root)
        if not reports.is_dir():
            click.echo("No reports yet. Reports appear in .dgov/reports/")
            return
        files = sorted(reports.glob("*.md"))
        if not files:
            click.echo("No reports yet. Reports appear in .dgov/reports/")
            return
        click.echo("Available reports:")
        for f in files:
            click.echo(f"  {f.stem}")
        return

    # Resolve the file path
    if os.path.isfile(name):
        doc_path = os.path.abspath(name)
    else:
        # Try .dgov/reports/<name>.md
        candidate = _reports_dir(session_root) / f"{name}.md"
        if candidate.is_file():
            doc_path = str(candidate)
        else:
            # Try without .md extension stripped
            candidate2 = _reports_dir(session_root) / name
            if candidate2.is_file():
                doc_path = str(candidate2)
            else:
                click.echo(f"Report not found: {name}")
                click.echo(f"Looked in: {_reports_dir(session_root)}")
                raise SystemExit(1)

    _open_glow_pane(doc_path)


def _open_glow_pane(doc_path: str) -> None:
    """Open glow in a tmux split pane to render a markdown file."""
    from dgov.tmux import _run, select_layout, send_command, set_title, split_pane

    title = f"[doc] {Path(doc_path).stem}"

    # Check if this doc is already open
    existing = _run(["list-panes", "-F", "#{pane_title}"], silent=True).splitlines()
    if title in existing:
        click.echo(f"Already open: {title}")
        return

    cmd = f"glow -p {shlex.quote(doc_path)}"
    pane_id = split_pane()
    send_command(pane_id, cmd)
    set_title(pane_id, title)

    _run(
        [
            "set-option",
            "-p",
            "-t",
            pane_id,
            "pane-border-format",
            " #[fg=colour141,bold]#{pane_index} #[fg=colour141]#{pane_title} ",
        ],
        silent=True,
    )

    select_layout("main-vertical")
    click.echo(f"Opened: {doc_path}")
