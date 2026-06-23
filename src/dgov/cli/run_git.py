"""Git preflight helpers for `dgov run`."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import click

from dgov.cli import _output, want_json
from dgov.git_status import porcelain_status_paths

_DIRTY_WORKTREE_STATUS = "blocked_by_dirty_worktree"
_DIRTY_PATH_LIMIT = 10


def _git_env(cwd: str | None = None) -> dict[str, str]:
    """Return clean git environment for local repo operations."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    if cwd is not None:
        env["PWD"] = cwd
    return env


def git_stdout(project_root: str, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        env=_git_env(project_root),
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def _working_tree_files(project_root: str) -> list[str]:
    """Return changed/untracked paths for a repo without assuming HEAD exists."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        text=True,
        env=_git_env(project_root),
        check=False,
    )
    return list(porcelain_status_paths(result.stdout, include_rename_sources=True))


def _create_bootstrap_commit(project_root: str, files: list[str]) -> None:
    """Create an initial snapshot commit for a repo that has no HEAD yet."""
    env = _git_env(project_root)
    env["GIT_AUTHOR_NAME"] = "dgov-bootstrap"
    env["GIT_AUTHOR_EMAIL"] = "bootstrap@dgov.local"
    env["GIT_COMMITTER_NAME"] = "dgov-bootstrap"
    env["GIT_COMMITTER_EMAIL"] = "bootstrap@dgov.local"

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore: bootstrap repo for dgov"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        click.echo("Error: failed to create bootstrap commit.", err=True)
        if details:
            click.echo(details, err=True)
        raise click.exceptions.Exit(code=1) from exc

    click.echo(f"Created bootstrap commit from current working tree ({len(files)} file(s)).")


def _require_git_repo(project_root: str) -> None:
    if not _is_git_repo(project_root):
        click.echo("Error: dgov run requires a git repository.", err=True)
        click.echo("Fix: run `git init` in this project first.", err=True)
        raise click.exceptions.Exit(code=1)


def _is_git_repo(project_root: str) -> bool:
    repo_check = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    return repo_check.returncode == 0


def _has_git_head(project_root: str) -> bool:
    head_check = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    return head_check.returncode == 0


def _raise_no_bootstrap_files() -> None:
    click.echo(
        "Error: repository has no commits and nothing to snapshot for dgov.",
        err=True,
    )
    click.echo("Fix: run `dgov init` or add files, then try again.", err=True)
    raise click.exceptions.Exit(code=1)


def _confirm_bootstrap_commit(files: Sequence[str]) -> bool:
    return click.confirm(
        (
            "Repository has no commits. Create a bootstrap commit from the current working "
            f"tree ({len(files)} file(s))?"
        ),
        default=True,
    )


def _raise_bootstrap_declined() -> None:
    click.echo(
        "Error: repository has no commits. dgov needs an initial snapshot before it can "
        "create worktrees.",
        err=True,
    )
    click.echo("Fix: create a bootstrap commit or commit manually, then try again.", err=True)
    raise click.exceptions.Exit(code=1)


def _ensure_bootstrap_commit(project_root: str, yes: bool) -> None:
    files = _working_tree_files(project_root)
    if not files:
        _raise_no_bootstrap_files()

    if all(path.startswith(".dgov/") for path in files):
        _create_bootstrap_commit(project_root, files)
        return

    if yes or not sys.stdin.isatty():
        _create_bootstrap_commit(project_root, files)
        return

    if _confirm_bootstrap_commit(files):
        _create_bootstrap_commit(project_root, files)
        return
    _raise_bootstrap_declined()


def _is_dispatch_compatible_path(path: str) -> bool:
    """Return True for generated/runtime dgov paths that may be dirty during dispatch."""
    return (
        path == ".dgov/plans/deployed.jsonl"
        or path == ".dgov/runs.log"
        or path.startswith(".dgov/state.db")
        or path.startswith(".dgov/out/")
        or path.startswith(".dgov/runtime/")
        or (path.startswith(".dgov/") and path.endswith("/_compiled.toml"))
    )


def dirty_worker_files(project_root: str) -> list[str]:
    dirty = _working_tree_files(project_root)
    return [f for f in dirty if not _is_dispatch_compatible_path(f)]


def _dirty_worktree_block_data(dirty: Sequence[str]) -> dict[str, object]:
    dirty_paths = list(dirty[:_DIRTY_PATH_LIMIT])
    # Emit both keys: `status` for generic JSON consumers, `dispatch_status`
    # for tooling that scans for the dispatch-phase outcome specifically.
    return {
        "status": _DIRTY_WORKTREE_STATUS,
        "dispatch_status": _DIRTY_WORKTREE_STATUS,
        "dirty_count": len(dirty),
        "dirty_paths": dirty_paths,
        "dirty_omitted": max(0, len(dirty) - len(dirty_paths)),
    }


def _raise_dirty_worktree(dirty: Sequence[str]) -> None:
    block = _dirty_worktree_block_data(dirty)
    if want_json():
        _output(block)
        raise click.exceptions.Exit(code=1)

    click.echo("Error: working tree has uncommitted changes.", err=True)
    click.echo(f"dispatch_status: {block['dispatch_status']}", err=True)
    click.echo(f"dirty_count: {block['dirty_count']}", err=True)
    click.echo(
        "Worktrees branch from HEAD — uncommitted files cause merge conflicts.",
        err=True,
    )
    click.echo("dirty_paths:", err=True)
    for path in cast(list[str], block["dirty_paths"]):
        click.echo(f"  {path}", err=True)
    click.echo(f"dirty_omitted: {block['dirty_omitted']}", err=True)
    click.echo("Fix: commit or stash your changes, then retry.", err=True)
    raise click.exceptions.Exit(code=1)


def ensure_git_ready(project_root: str, yes: bool = False) -> None:
    """Fail fast unless the current directory is a git repo with a clean working tree."""
    _require_git_repo(project_root)
    if not _has_git_head(project_root):
        _ensure_bootstrap_commit(project_root, yes)
        return

    dirty = dirty_worker_files(project_root)
    if dirty:
        _raise_dirty_worktree(dirty)


def block_dirty_committed_worktree(project_root: str) -> None:
    if not _is_git_repo(project_root) or not _has_git_head(project_root):
        return
    dirty = dirty_worker_files(project_root)
    if dirty:
        _raise_dirty_worktree(dirty)


def warn_if_archive_left_git_changes(project_root: str) -> None:
    changes = git_stdout(
        project_root,
        ["status", "--porcelain", "--untracked-files=all", "--", ".dgov/plans"],
    )
    if not changes:
        return
    click.echo(
        "  archive git changes: plan source was moved under .dgov/plans/archive; "
        "review and commit the archive move when appropriate.",
        err=True,
    )
