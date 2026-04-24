"""Clean subcommand — remove stale worktrees and disposable output directories."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from dgov.cli import cli
from dgov.project_root import resolve_project_root
from dgov.worktree import prune_orphans


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
    """Clean stale worktrees, output directories, and merged worker branches.

    Cleans three classes of cruft from prior runs:

    1. `.dgov/out/` output directories — deleted unconditionally.
    2. Orphan worker worktrees — directories under the sibling
       `.dgov-worktrees-<project>/` dir that git no longer tracks as live,
       plus fully-merged `dgov/*` branches with no attached worktree.
       Uses `git branch -d` (safe, merged-only); never touches unmerged work.
    3. Archived `.dgov/runtime/fix-plans/archive/` plans from resolved one-off
       fixes. Unresolved runtime fix plans remain operator-visible by design.

    Pass `--dry-run` to report what would be removed without touching anything.
    """
    project_root = resolve_project_root()
    dgov_dir = project_root / ".dgov"
    out_dir = dgov_dir / "out"
    runtime_fix_plans_dir = dgov_dir / "runtime" / "fix-plans"

    deleted_count = 0

    # 1. Clean .dgov/out/ — delete all directories
    if out_dir.exists():
        for item in out_dir.iterdir():
            if item.is_dir():
                try:
                    deleted_count += int(_delete_dir(item, dry_run))
                except Exception as exc:
                    click.echo(f"Error deleting {item}: {exc}", err=True)

    # 2. Prune orphan worktrees + merged dgov/* branches (git-level, safe).
    prune_counts = prune_orphans(str(project_root), dry_run=dry_run)
    verb = "Would prune" if dry_run else "Pruned"
    click.echo(
        f"{verb} {prune_counts['worktrees']} orphan worktree(s) "
        f"and {prune_counts['branches']} merged dgov/* branch(es)."
    )

    # 3. Clean resolved runtime fix-plan archives. Live runtime fix plans are preserved.
    if runtime_fix_plans_dir.exists():
        archive_dir = runtime_fix_plans_dir / "archive"
        if archive_dir.exists():
            for item in archive_dir.iterdir():
                if item.is_dir():
                    try:
                        deleted_count += int(_delete_dir(item, dry_run))
                    except Exception as exc:
                        click.echo(f"Error deleting {item}: {exc}", err=True)

    if dry_run:
        click.echo("Dry run complete. Use without --dry-run to delete.")
    else:
        click.echo(f"Clean complete. Deleted {deleted_count} directories.")
