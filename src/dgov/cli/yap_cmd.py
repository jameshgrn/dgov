"""CLI command: dgov yap — talk to dgov in natural language."""

from __future__ import annotations

import json
import os
from dataclasses import asdict

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.command("yap")
@click.argument("text", nargs=-1, required=True)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def yap_cmd(
    text: tuple[str, ...], project_root: str, session_root: str | None, as_json: bool
) -> None:
    """Talk to dgov in natural language."""
    from dgov.cli import _check_governor_context
    from dgov.yapper import yap

    _check_governor_context()
    full_text = " ".join(text)
    result = yap(full_text, os.path.abspath(project_root), session_root)
    if as_json:
        click.echo(json.dumps(asdict(result), default=str))
    else:
        click.echo(result.reply)
