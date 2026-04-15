"""Tests for dgov.worktree module.

Real git repos via tmp_path, no mocking.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dgov.types import Worktree
from dgov.worktree import (
    _worktrees_dir,
    commit_in_worktree,
    create_worktree,
    merge_worktree,
    prune_orphans,
    remove_worktree,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture
def git_repo(tmp_path):
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )
    return str(tmp_path)


class TestCreateWorktree:
    def test_creates_at_expected_path(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        assert wt.path.exists()
        assert ".dgov-worktrees-" in str(wt.path.parent)

    def test_returns_valid_worktree(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        assert isinstance(wt, Worktree)
        assert wt.path.is_dir()
        assert len(wt.commit) == 40

    def test_worktree_has_git_file(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        assert (wt.path / ".git").exists()

    def test_branch_name_pattern(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        assert wt.branch == "dgov/task-a"

    def test_idempotent_recreate(self, git_repo):
        slug = "task-a"
        wt1 = create_worktree(git_repo, slug)
        first_path = wt1.path
        (wt1.path / "marker.txt").write_text("old")

        wt2 = create_worktree(git_repo, slug)
        assert wt2.path == first_path
        assert not (wt2.path / "marker.txt").exists()

    def test_links_root_venv_into_worktree(self, git_repo):
        repo = Path(git_repo)
        (repo / ".venv").mkdir()

        wt = create_worktree(git_repo, "task-a")

        assert (wt.path / ".venv").is_symlink()
        assert (wt.path / ".venv").resolve() == (repo / ".venv").resolve()


class TestCommitInWorktree:
    def test_commit_with_file_claims(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        (wt.path / "new.py").write_text("x = 1\n")
        sha = commit_in_worktree(wt, "add new.py", file_claims=("new.py",))
        assert len(sha) == 40
        assert sha != wt.commit

    def test_commit_with_no_claims(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        (wt.path / "new.py").write_text("x = 1\n")
        sha = commit_in_worktree(wt, "add new.py")
        assert len(sha) == 40
        assert sha != wt.commit


class TestMergeWorktree:
    def test_merge_brings_file_to_root(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        (wt.path / "hello.py").write_text("print('hi')\n")
        commit_in_worktree(wt, "add hello.py", file_claims=("hello.py",))
        merge_worktree(git_repo, wt)
        assert (Path(git_repo) / "hello.py").exists()

    def test_ff_merge_returns_sha(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        (wt.path / "hello.py").write_text("print('hi')\n")
        commit_in_worktree(wt, "add hello.py", file_claims=("hello.py",))
        sha = merge_worktree(git_repo, wt)
        assert len(sha) == 40


class TestRemoveWorktree:
    def test_removes_directory(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        assert wt.path.exists()
        remove_worktree(git_repo, wt)
        assert not wt.path.exists()

    def test_deletes_branch(self, git_repo):
        wt = create_worktree(git_repo, "task-a")
        remove_worktree(git_repo, wt)
        res = subprocess.run(
            ["git", "branch", "--list", wt.branch],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert wt.branch not in res.stdout


class TestPruneOrphans:
    def test_noop_on_clean_repo(self, git_repo):
        """Nothing to clean → counts are zero."""
        result = prune_orphans(git_repo)
        assert result == {"worktrees": 0, "branches": 0}

    def test_removes_orphan_directory(self, git_repo):
        """A dir under the sibling worktrees root with no git entry is removed."""
        worktrees_dir = _worktrees_dir(git_repo)
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        orphan = worktrees_dir / "stale-task"
        orphan.mkdir()
        (orphan / "leftover.txt").write_text("debris\n")

        result = prune_orphans(git_repo)

        assert result["worktrees"] == 1
        assert not orphan.exists()

    def test_leaves_live_worktree_alone(self, git_repo):
        """A worktree git still tracks is not removed."""
        wt = create_worktree(git_repo, "live-task")
        try:
            result = prune_orphans(git_repo)
            assert result["worktrees"] == 0
            assert wt.path.exists()
        finally:
            remove_worktree(git_repo, wt)

    def test_deletes_merged_orphan_branch(self, git_repo):
        """A dgov/* branch with no worktree AND merged into HEAD is deleted."""
        wt = create_worktree(git_repo, "merge-me")
        (wt.path / "file.txt").write_text("contents\n")
        commit_in_worktree(wt, "add file", file_claims=("file.txt",))
        merge_worktree(git_repo, wt)
        # Simulate crash: remove worktree dir but leave the branch ref.
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(wt.path)],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        result = prune_orphans(git_repo)

        assert result["branches"] == 1
        branches = subprocess.run(
            ["git", "branch", "--list", "dgov/*"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert branches.stdout.strip() == ""

    def test_keeps_unmerged_orphan_branch(self, git_repo):
        """An unmerged dgov/* branch is NOT deleted (safety)."""
        wt = create_worktree(git_repo, "unmerged")
        (wt.path / "file.txt").write_text("contents\n")
        commit_in_worktree(wt, "unmerged change", file_claims=("file.txt",))
        # Remove the worktree dir WITHOUT merging the branch.
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(wt.path)],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        result = prune_orphans(git_repo)

        assert result["branches"] == 0
        branches = subprocess.run(
            ["git", "branch", "--list", "dgov/unmerged"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "dgov/unmerged" in branches.stdout

    def test_ignores_non_dgov_branches(self, git_repo):
        """Branches outside the dgov/* namespace are never touched."""
        subprocess.run(
            ["git", "branch", "feature/keep-me"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        prune_orphans(git_repo)

        branches = subprocess.run(
            ["git", "branch", "--list", "feature/keep-me"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "feature/keep-me" in branches.stdout

    def test_dry_run_reports_without_modifying(self, git_repo):
        """Dry-run counts orphans but does not remove them."""
        worktrees_dir = _worktrees_dir(git_repo)
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        orphan = worktrees_dir / "stale-task"
        orphan.mkdir()

        result = prune_orphans(git_repo, dry_run=True)

        assert result["worktrees"] == 1
        assert orphan.exists()
