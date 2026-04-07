"""Tests for dgov.worktree module.

Real git repos via tmp_path, no mocking.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from dgov.types import Worktree
from dgov.worktree import (
    commit_in_worktree,
    create_worktree,
    merge_worktree,
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
        from pathlib import Path as P

        assert (P(git_repo) / "hello.py").exists()

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
