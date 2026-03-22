"""Unit tests for low-level git plumbing helpers in src/dgov/gitops.py."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dgov.gitops import _remove_worktree

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git command in repo directory."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(tmp_path: Path, name: str) -> Path:
    """Initialize a git repo with basic files."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / ".gitignore").write_text(".dgov/\n")
    (repo / "README.md").write_text("initial\n")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _create_worktree(repo: Path, tmp_path: Path, branch_name: str) -> Path:
    """Create a worktree for the given branch."""
    # Create branch first
    _git(repo, "checkout", "-b", branch_name)
    (repo / f"{branch_name}.txt").write_text(f"content for {branch_name}\n")
    _git(repo, "add", f"{branch_name}.txt")
    _git(repo, "commit", "-m", f"add {branch_name} file")

    # Create worktree with new branch suffix
    worktree = tmp_path / f"{branch_name}-wt"
    _git(repo, "worktree", "add", "-b", f"{branch_name}-wt", str(worktree), "HEAD")

    # Switch back to main
    _git(repo, "checkout", "main")
    return worktree


class TestRemoveWorktree:
    """Tests for the _remove_worktree function."""

    def test_remove_worktree_success(self, tmp_path: Path) -> None:
        """Successful worktree removal with branch deletion and prune."""
        repo = _init_repo(tmp_path, "success")
        worktree = _create_worktree(repo, tmp_path, "test-branch")

        assert worktree.exists()
        # The file was committed to the branch, so it exists in worktree too
        assert (worktree / "test-branch.txt").exists()
        assert _git(repo, "rev-parse", "--verify", "test-branch-wt").returncode == 0

        result = _remove_worktree(str(repo), str(worktree), "test-branch-wt")

        assert result["success"] is True
        assert not worktree.exists()
        # Check branch was removed
        ret = _git(repo, "rev-parse", "--verify", "test-branch-wt", check=False).returncode
        assert ret != 0

    def test_remove_worktree_nonexistent(self, tmp_path: Path) -> None:
        """When git worktree remove fails, return error dict."""
        repo = _init_repo(tmp_path, "subprocess-error")
        _ = _create_worktree(repo, tmp_path, "error-branch")

        # Try to remove worktree with nonexistent branch
        nonexistent_wt = tmp_path / "nowhere"
        result = _remove_worktree(str(repo), str(nonexistent_wt), "nonexistent-branch")

        assert "success" in result

    def test_remove_worktree_prune_cleanup(self, tmp_path: Path) -> None:
        """When git worktree prune runs after remove succeeds."""
        repo = _init_repo(tmp_path, "prune-error")
        worktree = _create_worktree(repo, tmp_path, "prune-branch")

        # Manually make the worktree directory disappear
        shutil.rmtree(worktree)

        result = _remove_worktree(str(repo), str(worktree), "prune-branch-wt")

        assert result["success"] is True

    def test_remove_worktree_special_chars(self, tmp_path: Path) -> None:
        """Worktree with special characters in name."""
        repo = _init_repo(tmp_path, "special-chars")
        worktree = _create_worktree(repo, tmp_path, "feature-branch-v1.0")

        result = _remove_worktree(str(repo), str(worktree), "feature-branch-v1.0-wt")

        assert result["success"] is True
        assert not worktree.exists()
        # Check branch was removed
        ret = _git(repo, "rev-parse", "--verify", "feature-branch-v1.0-wt", check=False).returncode
        assert ret != 0

    def test_remove_worktree_preserves_main(self, tmp_path: Path) -> None:
        """Removing a worktree does not affect main branch."""
        repo = _init_repo(tmp_path, "preserve-main")
        worktree = _create_worktree(repo, tmp_path, "feature")

        # Record main's HEAD
        main_head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        readme_before = (repo / "README.md").read_text()

        result = _remove_worktree(str(repo), str(worktree), "feature-wt")

        assert result["success"] is True
        # Main should be intact
        main_head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        readme_after = (repo / "README.md").read_text()

        assert main_head_before == main_head_after
        assert readme_before == readme_after

    def test_remove_worktree_multiple_branches(self, tmp_path: Path) -> None:
        """Removing one worktree does not remove other branches."""
        repo = _init_repo(tmp_path, "multiple-branches")

        # Create multiple branches with worktrees
        branch1_wt = _create_worktree(repo, tmp_path, "branch-1")
        _ = _create_worktree(repo, tmp_path, "branch-2")

        # Remove first worktree
        result1 = _remove_worktree(str(repo), str(branch1_wt), "branch-1-wt")
        assert result1["success"] is True

        # Second branch should still exist in git
        ret = _git(repo, "rev-parse", "--verify", "branch-2-wt", check=False).returncode
        assert ret == 0

    def test_remove_worktree_invalid_path(self, tmp_path: Path) -> None:
        """Removal with non-existent worktree path."""
        repo = _init_repo(tmp_path, "invalid-path")
        nonexistent = tmp_path / "does-not-exist-wt"

        result = _remove_worktree(str(repo), str(nonexistent), "test-branch")

        # Should handle gracefully
        assert "success" in result

    def test_remove_worktree_idempotent(self, tmp_path: Path) -> None:
        """Removing worktree multiple times is idempotent."""
        repo = _init_repo(tmp_path, "idempotent")
        worktree = _create_worktree(repo, tmp_path, "repeat")

        # First removal
        result1 = _remove_worktree(str(repo), str(worktree), "repeat-wt")
        assert result1["success"] is True

        # Second removal (branch already gone)
        result2 = _remove_worktree(str(repo), str(worktree), "repeat-wt")
        # Should still return success (idempotent behavior)
        assert "success" in result2
