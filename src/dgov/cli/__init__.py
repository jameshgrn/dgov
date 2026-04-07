"""dgov CLI — headless governor surface."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import click

from dgov import __version__
from dgov.persistence import all_tasks, cleanup_zombies
from dgov.types import TaskState, Worktree
from dgov.worktree import remove_worktree

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dgov")


def want_json() -> bool:
    """Check if JSON output is requested via env var or context."""
    return os.environ.get("DGOV_JSON", "").strip() in ("1", "true", "yes")


def _output(data: dict) -> None:
    """Output data as JSON or human-readable."""
    if want_json():
        click.echo(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            click.echo(f"{key}: {value}")


@click.group(invoke_without_command=True)
@click.option("--json", is_flag=True, help="Output as JSON")
@click.version_option(version=__version__, prog_name="dgov")
@click.pass_context
def cli(
    ctx: click.Context,
    json: bool,
) -> None:
    """dgov — headless governor.

    \b
    USAGE:
      dgov                    Show status
      dgov status             Show status
      dgov run plan.toml      Run a plan
      dgov validate plan.toml Validate a plan without running
      dgov init               Bootstrap .dgov/project.toml
      dgov watch              Stream events
      dgov ledger add <cat>   Record bug, rule, or debt
      dgov compile <dir>      Compile a plan tree to _compiled.toml
      dgov plan status <dir>  Show pending vs deployed units
      dgov sentrux check      Run Sentrux architectural check

    Tasks run in isolated git worktrees. No tmux required.
    """
    if json:
        os.environ["DGOV_JSON"] = "1"

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` → show status
    _cmd_status(str(Path.cwd()))


@cli.command(name="status")
def status_cmd() -> None:
    """Show governor status — what's running now."""
    _cmd_status(str(Path.cwd()))


@cli.command(name="cleanup")
def cleanup_cmd() -> None:
    """Annihilate zombies — marks ACTIVE tasks as ABANDONED and removes worktrees."""
    project_root = str(Path.cwd())
    try:
        tasks = all_tasks(project_root)
        active = [t for t in tasks if t.get("state") == TaskState.ACTIVE]
        
        if not active:
            click.echo("No active tasks found. Everything is clean.")
            return

        click.echo(f"Cleaning up {len(active)} active tasks...")
        
        for t in active:
            slug = t.get("slug", "unknown")
            wt_path = t.get("worktree_path")
            branch = t.get("branch_name")
            
            if wt_path and branch:
                try:
                    wt = Worktree(path=Path(wt_path), branch=branch, commit="")
                    remove_worktree(project_root, wt)
                    click.echo(f"  [removed worktree] {slug}")
                except Exception as e:
                    click.echo(f"  [skip worktree] {slug}: {e}")

        count = cleanup_zombies(project_root)
        click.echo(f"Transitions complete: {count} tasks marked as ABANDONED.")
        
    except Exception as exc:
        click.echo(f"Cleanup failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1)


def _cmd_status(project_root: str) -> None:
    """Show governor status — what's running now."""
    try:
        tasks = all_tasks(project_root)
    except Exception as exc:
        _output({"status": "error", "message": str(exc)})
        return

    if not tasks:
        _output({"status": "idle", "tasks": 0})
        return

    active = [t for t in tasks if t.get("state") == "active"]
    _output(
        {
            "status": "active" if active else "idle",
            "tasks": len(tasks),
            "active": len(active),
            "task_list": [{"slug": t.get("slug"), "state": t.get("state")} for t in tasks[:10]],
        }
    )


# Register subcommand modules — must be at bottom after cli is defined
from dgov.cli import compile as _compile  # noqa: E402, F401
from dgov.cli import init as _init  # noqa: E402, F401
from dgov.cli import ledger as _ledger  # noqa: E402, F401
from dgov.cli import plan as _plan  # noqa: E402, F401
from dgov.cli import run as _run  # noqa: E402, F401
from dgov.cli import sentrux as _sentrux  # noqa: E402, F401
from dgov.cli import watch as _watch  # noqa: E402, F401
