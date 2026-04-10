"""Clean subcommand — remove stale worktrees and output directories."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from dgov.cli import cli
from dgov.persistence import all_tasks
from dgov.project_root import resolve_project_root


def _delete_dir(path: Path, dry_run: bool) -> bool:
    """Delete a directory or report what would be deleted."""
    if dry_run:
        click.echo(f"Would delete: {path}")
        return False
    shutil.rmtree(path)
    click.echo(f"Deleted: {path}")
    return True


@cli.command(name="clean")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
def clean_cmd(dry_run: bool) -> None:
    """Clean stale worktrees and output directories.

    Removes all directories from .dgov/out/ and removes worktrees
    from .dgov/worktrees/ that are not associated with active tasks.
    Also removes transient one-off fix plans from .dgov/runtime/fix-plans/.
    """
    project_root = resolve_project_root()
    dgov_dir = project_root / ".dgov"
    out_dir = dgov_dir / "out"
    worktrees_dir = dgov_dir / "worktrees"
    runtime_fix_plans_dir = dgov_dir / "runtime" / "fix-plans"

    # Collect active worktree paths from the database
    active_worktrees: set[str] = set()
    active_plan_names: set[str] = set()
    try:
        tasks = all_tasks(str(project_root))
        for task in tasks:
            if task.get("state") == "active":
                wt_path = task.get("worktree_path")
                if wt_path:
                    active_worktrees.add(Path(wt_path).resolve().as_posix())
                plan_name = task.get("plan_name")
                if plan_name:
                    active_plan_names.add(str(plan_name))
    except Exception as exc:
        click.echo(f"Error reading active tasks: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc

    deleted_count = 0

    # Clean .dgov/out/ - delete all directories
    if out_dir.exists():
        for item in out_dir.iterdir():
            if item.is_dir():
                try:
                    deleted_count += int(_delete_dir(item, dry_run))
                except Exception as exc:
                    click.echo(f"Error deleting {item}: {exc}", err=True)

    # Clean .dgov/worktrees/ - delete only inactive worktrees
    if worktrees_dir.exists():
        for item in worktrees_dir.iterdir():
            if item.is_dir():
                item_resolved = item.resolve().as_posix()
                if item_resolved not in active_worktrees:
                    try:
                        deleted_count += int(_delete_dir(item, dry_run))
                    except Exception as exc:
                        click.echo(f"Error deleting {item}: {exc}", err=True)
                else:
                    click.echo(f"Preserved (active): {item}")

    # Clean .dgov/runtime/fix-plans/ - delete generated fix plans and archives
    if runtime_fix_plans_dir.exists():
        archive_dir = runtime_fix_plans_dir / "archive"
        if archive_dir.exists():
            for item in archive_dir.iterdir():
                if item.is_dir():
                    try:
                        deleted_count += int(_delete_dir(item, dry_run))
                    except Exception as exc:
                        click.echo(f"Error deleting {item}: {exc}", err=True)

        for item in runtime_fix_plans_dir.iterdir():
            if not item.is_dir() or item.name == "archive":
                continue
            if item.name in active_plan_names:
                click.echo(f"Preserved (active): {item}")
                continue
            try:
                deleted_count += int(_delete_dir(item, dry_run))
            except Exception as exc:
                click.echo(f"Error deleting {item}: {exc}", err=True)

    if dry_run:
        click.echo("Dry run complete. Use without --dry-run to delete.")
    else:
        click.echo(f"Clean complete. Deleted {deleted_count} directories.")
