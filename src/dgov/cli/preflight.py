"""Local acceptance-gate preflight for hand edits."""

from __future__ import annotations

import click

from dgov.cli import _output, cli, want_json
from dgov.project_root import resolve_project_root
from dgov.settlement import preflight_sandbox


@cli.command(name="preflight")
def preflight_cmd() -> None:
    """Run settlement acceptance gates against local working-tree changes."""
    project_root = resolve_project_root()
    result = preflight_sandbox(project_root, str(project_root))

    if want_json():
        _output({
            "passed": result.passed,
            "project_root": str(project_root),
            "error": result.error,
        })
    elif result.passed:
        click.echo("Preflight passed.")
    else:
        click.echo(f"Preflight failed:\n{result.error}", err=True)

    if not result.passed:
        raise click.exceptions.Exit(code=1)
