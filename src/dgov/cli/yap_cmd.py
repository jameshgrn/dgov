"""CLI command: dgov yap — talk to dgov in natural language."""

from __future__ import annotations

import json
import os
import readline
import sys
import threading
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


def _spinner(stop: threading.Event) -> None:
    """Animate a dot spinner on stderr until stop is set."""
    frames = ["\u28fe", "\u28f7", "\u28ef", "\u28df", "\u287f", "\u28bf", "\u28fb", "\u28fd"]
    i = 0
    while not stop.wait(0.1):
        sys.stderr.write(f"\r  \033[2m{frames[i % len(frames)]} thinking\033[0m")
        sys.stderr.flush()
        i += 1
    sys.stderr.write("\r\033[2K")
    sys.stderr.flush()


_ACTION_STYLES = {
    "dispatched": ("green", True),
    "queued": ("yellow", False),
    "noted": ("yellow", False),
    "answered": ("cyan", False),
    "ack": ("dim", False),
    "error": ("red", True),
}


def _show_queue(session_root: str) -> None:
    """Print the dispatch queue."""
    from dgov.yapper import read_dispatch_queue

    items = read_dispatch_queue(session_root)
    if not items:
        click.secho("  Queue is empty.", fg="dim")
        return
    click.secho(f"  {len(items)} queued:", fg="yellow")
    for i, item in enumerate(items, 1):
        summary = item.get("summary", "?")
        agent = item.get("agent_hint") or "auto"
        click.echo(f"    {i}. [{agent}] {summary}")


def _drain_queue(project_root: str, session_root: str, yap_session: object) -> None:
    """Dispatch all queued items as LT-GOVs."""
    from dgov.yapper import clear_dispatch_queue, read_dispatch_queue, yap

    items = read_dispatch_queue(session_root)
    if not items:
        click.secho("  Queue is empty.", fg="dim")
        return
    count = clear_dispatch_queue(session_root)
    click.secho(f"  Draining {count} items...", fg="yellow")
    for item in items:
        text = item.get("text", item.get("summary", ""))
        # Re-yap with urgency forced to "now"
        result = yap(
            text,
            project_root,
            session_root,
            session=yap_session,  # type: ignore[arg-type]
        )
        icon, color = _CATEGORY_STYLES.get(result.category, (" ", "white"))
        click.secho(f"  {icon} ", fg=color, bold=True, nl=False)
        click.echo(result.reply)


def _handle_slash(line: str, project_root: str, session_root: str, yap_session: object) -> bool:
    """Handle /commands. Returns True if handled."""
    cmd = line.lstrip("/").split()[0].lower() if line.startswith("/") else ""
    if not cmd:
        return False
    if cmd == "queue":
        _show_queue(session_root)
        return True
    if cmd == "drain":
        _drain_queue(project_root, session_root, yap_session)
        return True
    if cmd == "help":
        click.secho("  /queue", fg="cyan", nl=False)
        click.echo("  — show queued dispatches")
        click.secho("  /drain", fg="cyan", nl=False)
        click.echo("  — dispatch all queued items as LT-GOVs")
        return True
    click.secho(f"  Unknown command: /{cmd}", fg="red")
    return True


def _repl(project_root: str, session_root: str | None, as_json: bool) -> None:
    """Interactive yapper REPL."""
    from dgov.yapper import YapperSession, yap

    _setup_readline()
    session = YapperSession()
    sr = session_root or project_root
    click.secho("dgov yapper", fg="green", bold=True, nl=False)
    click.secho("  ctrl-d to exit  /help for commands", fg="bright_black")
    while True:
        try:
            line = input("\001\033[1;32m\002> \001\033[0m\002").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            break
        if not line:
            continue
        if line.startswith("/"):
            _handle_slash(line, project_root, sr, session)
            continue

        stop = threading.Event()
        t = threading.Thread(target=_spinner, args=(stop,), daemon=True)
        t.start()
        try:
            result = yap(line, project_root, session_root, session=session)
        finally:
            stop.set()
            t.join()

        if as_json:
            click.echo(json.dumps(asdict(result), default=str))
        else:
            icon, color = _CATEGORY_STYLES.get(result.category, (" ", "white"))
            action_color, bold = _ACTION_STYLES.get(result.action, ("white", False))
            click.secho(f"  {icon} ", fg=color, bold=True, nl=False)
            click.secho(result.reply, fg=action_color, bold=bold)
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
