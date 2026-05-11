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


def _clean_child_dirs(path: Path, dry_run: bool) -> int:
    deleted_count = 0
    if not path.exists():
        return deleted_count
    for item in path.iterdir():
        if not item.is_dir():
            continue
        try:
            deleted_count += int(_delete_dir(item, dry_run))
        except Exception as exc:
            click.echo(f"Error deleting {item}: {exc}", err=True)
    return deleted_count


def _prune_orphan_worktrees(project_root: Path, dry_run: bool) -> None:
    prune_counts = prune_orphans(str(project_root), dry_run=dry_run)
    verb = "Would prune" if dry_run else "Pruned"
    click.echo(
        f"{verb} {prune_counts['worktrees']} orphan worktree(s) "
        f"and {prune_counts['branches']} merged dgov/* branch(es)."
    )


def _runtime_fix_plan_archive(dgov_dir: Path) -> Path:
    return dgov_dir / "runtime" / "fix-plans" / "archive"


def _echo_clean_complete(dry_run: bool, deleted_count: int) -> None:
    if dry_run:
        click.echo("Dry run complete. Use without --dry-run to delete.")
    else:
        click.echo(f"Clean complete. Deleted {deleted_count} directories.")


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

    deleted_count = _clean_child_dirs(dgov_dir / "out", dry_run)
    _prune_orphan_worktrees(project_root, dry_run)
    deleted_count += _clean_child_dirs(_runtime_fix_plan_archive(dgov_dir), dry_run)

    _echo_clean_complete(dry_run, deleted_count)
