"""CLI command: dgov briefing — on-demand document viewer via glow."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION

_SKIP_NAMES = frozenset(
    {
        "readme",
        "claude",
        "gemini",
        "handover",
        "changelog",
        "contributing",
        "license",
    }
)


def _find_reports(project_root: str, session_root: str) -> list[Path]:
    """Collect .md reports from .dgov/reports/, repo root, and docs/."""
    seen: set[str] = set()
    results: list[Path] = []
    search = [
        Path(session_root) / ".dgov" / "reports",
        Path(project_root),
        Path(project_root) / "docs",
    ]
    for d in search:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            key = f.stem.lower()
            if key in _SKIP_NAMES:
                continue
            if key not in seen:
                seen.add(key)
                results.append(f)
    return results


def _resolve_report(name: str, project_root: str, session_root: str) -> str | None:
    """Resolve a report name to an absolute file path."""
    # Absolute or relative path given directly
    if os.path.isfile(name):
        return os.path.abspath(name)

    # Search all report locations
    search = [
        Path(session_root) / ".dgov" / "reports",
        Path(project_root),
        Path(project_root) / "docs",
    ]
    for d in search:
        for suffix in (f"{name}.md", name):
            candidate = d / suffix
            if candidate.is_file():
                return str(candidate)
    return None


@click.group("briefing", invoke_without_command=True)
@click.argument("name", required=False)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.pass_context
def briefing_cmd(ctx, name: str | None, project_root: str, session_root: str | None) -> None:
    """View reports and documents with glow.

    \b
    dgov briefing                  # list available reports
    dgov briefing plumbing-audit   # open a report in glow window
    dgov briefing /path/to/doc.md  # open any markdown file
    """
    if ctx.invoked_subcommand is not None:
        return

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    if name is None:
        reports = _find_reports(project_root, session_root)
        if not reports:
            click.echo("No reports found.")
            return
        click.echo("Available reports:")
        for f in reports:
            click.echo(f"  {f.stem}")
        return

    doc_path = _resolve_report(name, project_root, session_root)
    if doc_path is None:
        click.echo(f"Report not found: {name}")
        raise SystemExit(1)

    _open_glow_window(doc_path)


def _open_glow_window(doc_path: str) -> None:
    """Open glow in a background tmux window."""
    from dgov.tmux import _run, send_command

    title = f"[doc] {Path(doc_path).stem}"

    # Check if this doc is already open in any window
    existing = _run(["list-windows", "-F", "#{window_name}"], silent=True).splitlines()
    if title in existing:
        # Switch to it
        _run(["select-window", "-t", f"={title}"], silent=True)
        click.echo(f"Switched to: {title}")
        return

    cmd = f"glow -p {shlex.quote(doc_path)}"
    _run(["new-window", "-d", "-n", title], silent=True)
    pane_id = (
        _run(["list-panes", "-t", f"={title}", "-F", "#{pane_id}"], silent=True)
        .strip()
        .splitlines()[0]
    )
    send_command(pane_id, cmd)

    click.echo(f"Opened: {doc_path} (window: {title})")
