"""Tests for dgov.worktree module.

Real git repos via tmp_path, no mocking.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dgov.types import Worktree
from dgov.worktree import (
    _worktrees_dir,
    commit_in_worktree,
    create_worktree,
    merge_worktree,
    prepare_worktree,
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


@pytest.mark.unit
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

    def test_idempotent_recreate_after_orphaned_metadata(self, git_repo):
        """Retry after interrupted cleanup: dir deleted but git metadata remains."""
        slug = "retry-task"
        wt1 = create_worktree(git_repo, slug)
        first_path = wt1.path

        # Make an unmerged commit (simulates real work)
        (wt1.path / "file.txt").write_text("contents\n")
        commit_in_worktree(wt1, "unmerged change", file_claims=("file.txt",))

        # Simulate interrupted cleanup: delete directory directly without git worktree remove
        shutil.rmtree(wt1.path)
        # Note: git still has worktree metadata at this point

        # Retry with same slug should succeed and create fresh worktree
        wt2 = create_worktree(git_repo, slug)
        assert wt2.path == first_path
        assert wt2.path.exists()
        # Fresh worktree should not have the uncommitted file
        assert not (wt2.path / "file.txt").exists()

    def test_links_root_venv_into_worktree(self, git_repo):
        repo = Path(git_repo)
        (repo / ".venv").mkdir()

        wt = create_worktree(git_repo, "task-a")

        assert (wt.path / ".venv").is_symlink()
        assert (wt.path / ".venv").resolve() == (repo / ".venv").resolve()

    def test_skips_shared_venv_link_for_pyproject_repo(self, git_repo):
        repo = Path(git_repo)
        (repo / ".venv").mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")

        wt = create_worktree(git_repo, "task-a")

        assert not (wt.path / ".venv").exists()


@pytest.mark.unit
class TestPrepareWorktree:
    def test_python_pyproject_runs_uv_sync_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        (wt_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        (wt_path / "uv.lock").write_text("lock = 1\n")
        wt = Worktree(path=wt_path, branch="dgov/task-a", commit="abc123")
        calls: list[tuple[object, ...]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(tuple(cmd) if isinstance(cmd, list) else (cmd,))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("dgov.worktree.subprocess.run", _fake_run)

        prepare_worktree(wt, language="python")

        assert calls == [("uv", "sync", "--locked")]

    def test_setup_cmd_takes_precedence_over_uv_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        (wt_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        wt = Worktree(path=wt_path, branch="dgov/task-a", commit="abc123")
        calls: list[str] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("dgov.worktree.subprocess.run", _fake_run)

        prepare_worktree(wt, language="python", setup_cmd="echo prepare")

        assert calls == ["echo prepare"]

    def test_prepare_worktree_surfaces_actionable_uv_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        (wt_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0.1.0'\n")
        wt = Worktree(path=wt_path, branch="dgov/task-a", commit="abc123")

        def _missing_uv(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr("dgov.worktree.subprocess.run", _missing_uv)

        with pytest.raises(RuntimeError, match="install uv"):
            prepare_worktree(wt, language="python")


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
class TestIntegrationCandidate:
    """Tests for integration candidate creation and validation."""

    def test_create_integration_candidate_success(self, git_repo):
        """Successfully replay task commits onto current HEAD."""
        from dgov.worktree import (
            create_integration_candidate,
            create_worktree,
            remove_worktree,
        )

        # Create task worktree with a commit
        task_wt = create_worktree(git_repo, "task-a")
        (task_wt.path / "hello.py").write_text("print('hello')\n")
        commit_in_worktree(task_wt, "add hello.py", file_claims=("hello.py",))

        # Create integration candidate
        result = create_integration_candidate(git_repo, task_wt, "task-a-candidate")

        assert result.passed is True
        assert result.candidate_path is not None
        assert result.candidate_path.exists()
        assert len(result.candidate_sha) == 40
        # File should be replayed onto candidate
        assert (result.candidate_path / "hello.py").exists()

        # Clean up
        from dgov.worktree import remove_integration_candidate

        remove_integration_candidate(git_repo, result.candidate_path)
        remove_worktree(git_repo, task_wt)

    def test_create_integration_candidate_no_commits(self, git_repo):
        """Fail gracefully when task worktree has no commits to replay."""
        from dgov.worktree import create_integration_candidate, create_worktree

        # Create task worktree but don't commit anything
        task_wt = create_worktree(git_repo, "task-empty")

        result = create_integration_candidate(git_repo, task_wt, "task-empty-candidate")

        assert result.passed is False
        assert result.error is not None
        assert "No commits to replay" in result.error

        # Clean up
        from dgov.worktree import remove_worktree

        remove_worktree(git_repo, task_wt)

    def test_create_integration_candidate_leaves_repo_clean_on_failure(self, git_repo):
        """If replay fails, main repo stays clean."""
        from dgov.worktree import (
            create_integration_candidate,
            create_worktree,
            remove_worktree,
        )

        # Create initial task worktree with a commit
        task_wt = create_worktree(git_repo, "task-base")
        (task_wt.path / "base.py").write_text("x = 1\n")
        commit_in_worktree(task_wt, "add base", file_claims=("base.py",))
        merge_worktree(git_repo, task_wt)

        # Create second task worktree that modifies the same file
        task_wt2 = create_worktree(git_repo, "task-conflict")
        (task_wt2.path / "base.py").write_text("x = 2\n")  # Different content
        failed_sha = commit_in_worktree(task_wt2, "modify base", file_claims=("base.py",))

        # Meanwhile, modify the file on main to create a conflict
        (Path(git_repo) / "base.py").write_text("x = 3\n")
        env = {**os.environ, **_GIT_ENV}
        subprocess.run(
            ["git", "add", "base.py"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", "change on main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            env=env,
        )
        target_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

        # Now try to create integration candidate - should fail
        result = create_integration_candidate(git_repo, task_wt2, "task-conflict-candidate")

        assert result.passed is False
        assert result.error is not None
        assert "Replay failed" in result.error
        assert "task-conflict-candidate" in result.error
        assert task_wt2.branch in result.error
        assert target_head[:8] in result.error
        assert failed_sha[:8] in result.error
        assert "base.py" in result.error
        assert result.target_head_sha == target_head
        assert result.failed_commit_sha == failed_sha
        assert result.conflict_files == ("base.py",)
        assert result.conflict_marker_counts["base.py"] >= 1

        # Verify main repo is clean (no partial cherry-pick state)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            env=env,
        )
        assert status.stdout.strip() == ""

        # Clean up
        remove_worktree(git_repo, task_wt2)

    def test_create_integration_candidate_at_current_head(self, git_repo):
        """Candidate is rooted at current HEAD, not task base."""
        from dgov.worktree import (
            create_integration_candidate,
            create_worktree,
            remove_worktree,
        )

        # Create task worktree
        task_wt = create_worktree(git_repo, "task-head")
        (task_wt.path / "task.py").write_text("# task code\n")
        commit_in_worktree(task_wt, "add task code", file_claims=("task.py",))

        # Add new commit on main while task is working
        env = {**os.environ, **_GIT_ENV}
        (Path(git_repo) / "main.py").write_text("# main code\n")
        subprocess.run(
            ["git", "add", "main.py"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", "progress on main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            env=env,
        )

        # Create integration candidate
        result = create_integration_candidate(git_repo, task_wt, "task-head-candidate")

        assert result.passed is True
        assert result.candidate_path is not None
        # Candidate should have both main.py (from HEAD) and task.py (from replay)
        assert (result.candidate_path / "main.py").exists()
        assert (result.candidate_path / "task.py").exists()

        # Clean up
        from dgov.worktree import remove_integration_candidate

        remove_integration_candidate(git_repo, result.candidate_path)
        remove_worktree(git_repo, task_wt)
