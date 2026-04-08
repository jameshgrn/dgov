"""dgov CLI — headless governor surface."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import click

from dgov import __version__
from dgov.persistence import all_tasks, cleanup_zombies, prune_history
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
      dgov prune              Prune historical (abandoned/closed) tasks
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
@click.option(
    "--all", "show_all", is_flag=True, help="Show all tasks including abandoned/closed history"
)
def status_cmd(show_all: bool) -> None:
    """Show governor status — what's running now."""
    _cmd_status(str(Path.cwd()), show_all=show_all)


@cli.command(name="cleanup")
def cleanup_cmd() -> None:
    """Annihilate zombies — marks ACTIVE tasks ABANDONED and removes orphaned worktree branches."""
    project_root = str(Path.cwd())
    try:
        tasks = all_tasks(project_root)

        # ACTIVE: in-flight tasks from a crashed run
        # FAILED/ABANDONED: preserved-but-rejected worktrees whose git branches were never deleted
        needs_wt_cleanup = [
            t
            for t in tasks
            if t.get("state") in (TaskState.ACTIVE, TaskState.FAILED, TaskState.ABANDONED)
            and t.get("worktree_path")
            and t.get("branch_name")
        ]

        if not needs_wt_cleanup:
            click.echo("No active tasks found. Everything is clean.")
            return

        click.echo(f"Cleaning up {len(needs_wt_cleanup)} tasks...")

        for t in needs_wt_cleanup:
            slug = t.get("slug", "unknown")
            wt_path = t.get("worktree_path")
            branch = t.get("branch_name")

            if not (wt_path and branch):
                continue
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
        raise click.exceptions.Exit(code=1) from exc


@cli.command(name="archive-plan")
@click.argument("name")
def archive_plan_cmd(name: str) -> None:
    """Manually archive a plan directory to .dgov/plans/archive/<name>."""
    from dgov.archive import archive_plan

    plan_dir = Path(".dgov") / "plans" / name
    if not plan_dir.exists():
        click.echo(f"Error: Plan not found: {plan_dir}", err=True)
        raise click.exceptions.Exit(code=1)
    archive_dir = plan_dir.parent / "archive"
    if (archive_dir / name).exists():
        click.echo(f"Error: Archive already exists: {archive_dir / name}", err=True)
        raise click.exceptions.Exit(code=1)
    dest = archive_plan(plan_dir)
    click.echo(f"Archived to {dest}")


@cli.command(name="prune")
def prune_cmd() -> None:
    """Prune historical tasks — removes abandoned and closed records."""
    project_root = str(Path.cwd())
    try:
        count = prune_history(project_root)
        if count == 0:
            click.echo("Nothing to prune.")
        else:
            click.echo(f"Pruned {count} historical task(s).")
    except Exception as exc:
        click.echo(f"Prune failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc


# States that represent settled history — not live governor state
_HISTORICAL_STATES = frozenset({"abandoned", "closed"})


def _cmd_status(project_root: str, show_all: bool = False) -> None:
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
    visible = tasks if show_all else [t for t in tasks if t.get("state") not in _HISTORICAL_STATES]

    if want_json():
        click.echo(
            json.dumps(
                {
                    "status": "active" if active else "idle",
                    "tasks": len(tasks),
                    "active": len(active),
                    "task_list": [
                        {"slug": t.get("slug"), "state": t.get("state")} for t in visible
                    ],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"status: {'active' if active else 'idle'}")
        click.echo(f"tasks: {len(tasks)} total")
        click.echo(f"active: {len(active)}")
        if visible:
            click.echo("tasks:")
            for t in visible:
                state = t.get("state", "?")
                slug = t.get("slug", "?")
                click.echo(f"  {state:14s}  {slug}")
        elif not show_all:
            click.echo("  (no live tasks — use --all to show history)")


# Register subcommand modules — must be at bottom after cli is defined
from dgov.cli import (  # noqa: E402
    clean as clean,
    compile as compile,
    init as init,
    ledger as ledger,
    plan as plan,
    run as run,
    sentrux as sentrux,
    watch as watch,
)
