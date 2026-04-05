"""Tests for worktree creation, merge, removal, and orphan pruning."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dgov.worktree import (
    _worktrees_dir,
    create_worktree,
    merge_worktree,
    prune_orphans,
    remove_worktree,
)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Fresh git repo with one initial commit."""
    env = {
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": subprocess.os.environ["PATH"],
    }

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    _git("init", "-b", "main")
    _git("config", "user.name", "test")
    _git("config", "user.email", "test@test.local")
    (tmp_path / "README.md").write_text("init\n")
    _git("add", ".")
    _git("commit", "-m", "initial commit")
    return tmp_path


def test_prune_noop_on_clean_repo(git_repo: Path) -> None:
    """Nothing to clean = counts are zero."""
    result = prune_orphans(str(git_repo))
    assert result == {"worktrees": 0, "branches": 0}


def test_prune_removes_orphan_directory(git_repo: Path) -> None:
    """A dir in the sibling worktrees dir with no git entry is removed."""
    worktrees_dir = _worktrees_dir(str(git_repo))
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    orphan = worktrees_dir / "stale-task"
    orphan.mkdir()
    (orphan / "leftover.txt").write_text("debris\n")

    result = prune_orphans(str(git_repo))

    assert result["worktrees"] == 1
    assert not orphan.exists()


def test_prune_leaves_live_worktree_alone(git_repo: Path) -> None:
    """A worktree git tracks is not removed."""
    wt = create_worktree(str(git_repo), "live-task")

    result = prune_orphans(str(git_repo))

    assert result["worktrees"] == 0
    assert wt.path.exists()
    # cleanup
    remove_worktree(str(git_repo), wt)


def test_prune_deletes_merged_orphan_branch(git_repo: Path) -> None:
    """A dgov/* branch with no worktree AND merged into HEAD is deleted."""
    wt = create_worktree(str(git_repo), "merge-me")
    (wt.path / "file.txt").write_text("contents\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "x"],
        cwd=wt.path,
        check=True,
        capture_output=True,
    )
    merge_worktree(str(git_repo), wt)
    # Remove the worktree dir but leave the branch behind (simulates crash)
    subprocess.run(
        ["git", "worktree", "remove", "-f", str(wt.path)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = prune_orphans(str(git_repo))

    assert result["branches"] == 1
    branches = subprocess.run(
        ["git", "branch", "--list", "dgov/*"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ""


def test_prune_keeps_unmerged_orphan_branch(git_repo: Path) -> None:
    """An unmerged dgov/* branch is NOT deleted (safety)."""
    wt = create_worktree(str(git_repo), "unmerged")
    (wt.path / "file.txt").write_text("contents\n")
    subprocess.run(["git", "add", "."], cwd=wt.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "unmerged"],
        cwd=wt.path,
        check=True,
        capture_output=True,
    )
    # Remove worktree but DON'T merge the branch
    subprocess.run(
        ["git", "worktree", "remove", "-f", str(wt.path)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = prune_orphans(str(git_repo))

    assert result["branches"] == 0
    branches = subprocess.run(
        ["git", "branch", "--list", "dgov/*"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "dgov/unmerged" in branches.stdout


def test_prune_ignores_non_dgov_branches(git_repo: Path) -> None:
    """Branches outside the dgov/* namespace are never touched."""
    subprocess.run(
        ["git", "branch", "feature/keep-me"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    prune_orphans(str(git_repo))

    branches = subprocess.run(
        ["git", "branch", "--list", "feature/*"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "feature/keep-me" in branches.stdout
