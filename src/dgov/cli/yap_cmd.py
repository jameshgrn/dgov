"""CLI command: dgov yap — talk to dgov in natural language."""

from __future__ import annotations

import json
import os
import readline
import sys
from dataclasses import asdict
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION

_CATEGORY_STYLES = {
    "COMMAND": ("\u2192", "green"),
    "IDEA": ("\u2726", "yellow"),
    "QUESTION": ("?", "cyan"),
    "CHATTER": ("\u00b7", "dim"),
}

_HISTORY_FILE = Path("~/.dgov/yap_history").expanduser()


def _setup_readline() -> None:
    """Load readline history and configure tab completion."""
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(_HISTORY_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(500)


def _save_readline() -> None:
    try:
        readline.write_history_file(_HISTORY_FILE)
    except OSError:
        pass


def _repl(project_root: str, session_root: str | None, as_json: bool) -> None:
    """Interactive yapper REPL."""
    from dgov.yapper import yap

    _setup_readline()
    click.secho("dgov yapper", fg="green", bold=True, nl=False)
    click.secho("  ctrl-d to exit", fg="bright_black")
    while True:
        try:
            line = input("\001\033[1;32m\002> \001\033[0m\002").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            break
        if not line:
            continue
        result = yap(line, project_root, session_root)
        if as_json:
            click.echo(json.dumps(asdict(result), default=str))
        else:
            icon, color = _CATEGORY_STYLES.get(result.category, (" ", "white"))
            click.secho(f"  {icon} ", fg=color, bold=True, nl=False)
            click.echo(result.reply)
    _save_readline()


@click.command("yap")
@click.argument("text", nargs=-1, required=False)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def yap_cmd(
    text: tuple[str, ...], project_root: str, session_root: str | None, as_json: bool
) -> None:
    """Talk to dgov in natural language. No args starts a REPL."""
    from dgov.cli import _check_governor_context
    from dgov.yapper import yap

    _check_governor_context()
    project_root = os.path.abspath(project_root)

    if not text:
        if not sys.stdin.isatty():
            # Pipe mode: read each line as a yap
            for line in sys.stdin:
                line = line.strip()
                if line:
                    result = yap(line, project_root, session_root)
                    if as_json:
                        click.echo(json.dumps(asdict(result), default=str))
                    else:
                        click.echo(result.reply)
            return
        _repl(project_root, session_root, as_json)
        return

    full_text = " ".join(text)
    result = yap(full_text, project_root, session_root)
    if as_json:
        click.echo(json.dumps(asdict(result), default=str))
    else:
        click.echo(result.reply)
