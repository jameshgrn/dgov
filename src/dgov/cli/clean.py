"""Clean subcommand — remove stale worktrees and output directories."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from dgov.cli import cli
from dgov.persistence import all_tasks


@cli.command(name="clean")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
def clean_cmd(dry_run: bool) -> None:
    """Clean stale worktrees and output directories.

    Removes all directories from .dgov/out/ and removes worktrees
    from .dgov/worktrees/ that are not associated with active tasks.
    """
    project_root = Path.cwd()
    dgov_dir = project_root / ".dgov"
    out_dir = dgov_dir / "out"
    worktrees_dir = dgov_dir / "worktrees"

    # Collect active worktree paths from the database
    active_worktrees: set[str] = set()
    try:
        tasks = all_tasks(str(project_root))
        for task in tasks:
            if task.get("state") == "active":
                wt_path = task.get("worktree_path")
                if wt_path:
                    active_worktrees.add(Path(wt_path).resolve().as_posix())
    except Exception as exc:
        click.echo(f"Error reading active tasks: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc

    deleted_count = 0

    # Clean .dgov/out/ - delete all directories
    if out_dir.exists():
        for item in out_dir.iterdir():
            if item.is_dir():
                if dry_run:
                    click.echo(f"Would delete: {item}")
                else:
                    try:
                        shutil.rmtree(item)
                        click.echo(f"Deleted: {item}")
                        deleted_count += 1
                    except Exception as exc:
                        click.echo(f"Error deleting {item}: {exc}", err=True)

    # Clean .dgov/worktrees/ - delete only inactive worktrees
    if worktrees_dir.exists():
        for item in worktrees_dir.iterdir():
            if item.is_dir():
                item_resolved = item.resolve().as_posix()
                if item_resolved not in active_worktrees:
                    if dry_run:
                        click.echo(f"Would delete: {item}")
                    else:
                        try:
                            shutil.rmtree(item)
                            click.echo(f"Deleted: {item}")
                            deleted_count += 1
                        except Exception as exc:
                            click.echo(f"Error deleting {item}: {exc}", err=True)
                else:
                    click.echo(f"Preserved (active): {item}")

    if dry_run:
        click.echo("Dry run complete. Use without --dry-run to delete.")
    else:
        click.echo(f"Clean complete. Deleted {deleted_count} directories.")
