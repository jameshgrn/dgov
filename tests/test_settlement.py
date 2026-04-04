"""Tests for settlement gates — review, autofix, validate.

Uses real git repos (tmp_path) to test the actual git operations
that gate worker output before merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from dgov.settlement import (
    autofix_sandbox,
    review_sandbox,
    validate_sandbox,
)

# ---------------------------------------------------------------------------
# Helpers — create real git repos in tmp_path
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
    )


def _init_repo(path: Path) -> str:
    """Create a git repo with one initial commit. Returns base SHA."""
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    sha = _git(path, "rev-parse", "HEAD").stdout.strip()
    return sha


def _add_tracked_file(path: Path, filename: str, content: str) -> None:
    """Add a tracked file to the repo (committed)."""
    (path / filename).write_text(content)
    _git(path, "add", filename)
    _git(path, "commit", "-m", f"add {filename}")


def _modify_tracked(path: Path, filename: str, content: str) -> None:
    """Modify an existing tracked file (unstaged change visible to git diff)."""
    (path / filename).write_text(content)


# ---------------------------------------------------------------------------
# review_sandbox
# ---------------------------------------------------------------------------


class TestReviewSandbox:
    def test_pass_with_tracked_changes(self, tmp_path: Path):
        """Review passes when tracked files are modified."""
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "src.py", "x = 1\n")
        _modify_tracked(tmp_path, "src.py", "x = 2\n")
        result = review_sandbox(tmp_path)
        assert result.passed
        assert result.verdict == "ok"
        assert "src.py" in result.actual_files

    def test_pass_with_new_untracked_files(self, tmp_path: Path):
        """Review passes when worker creates new files."""
        _init_repo(tmp_path)
        (tmp_path / "new_file.py").write_text("x = 1\n")
        result = review_sandbox(tmp_path)
        assert result.passed
        assert "new_file.py" in result.actual_files

    def test_empty_diff_fails(self, tmp_path: Path):
        _init_repo(tmp_path)
        result = review_sandbox(tmp_path)
        assert not result.passed
        assert result.verdict == "empty_diff"

    def test_scope_violation(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _add_tracked_file(tmp_path, "unclaimed.py", "y = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        _modify_tracked(tmp_path, "unclaimed.py", "y = 2\n")
        result = review_sandbox(tmp_path, claimed_files=["claimed.py"])
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "unclaimed.py" in (result.error or "")

    def test_scope_pass_when_within_claims(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        result = review_sandbox(tmp_path, claimed_files=["claimed.py"])
        assert result.passed

    def test_scope_pass_new_file_claimed(self, tmp_path: Path):
        """New untracked file within claimed scope passes."""
        _init_repo(tmp_path)
        (tmp_path / "new.py").write_text("x = 1\n")
        result = review_sandbox(tmp_path, claimed_files=["new.py"])
        assert result.passed

    def test_no_scope_check_without_claims(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "anything.py", "x = 1\n")
        _modify_tracked(tmp_path, "anything.py", "x = 2\n")
        result = review_sandbox(tmp_path, claimed_files=None)
        assert result.passed

    def test_diff_too_large(self, tmp_path: Path):
        _init_repo(tmp_path)
        for i in range(20):
            _add_tracked_file(tmp_path, f"file{i}.py", f"x = {i}\n")
        for i in range(20):
            _modify_tracked(tmp_path, f"file{i}.py", f"x = {i + 100}\n")
        result = review_sandbox(tmp_path, max_diff_lines=5)
        assert not result.passed
        assert result.verdict == "diff_too_large"


# ---------------------------------------------------------------------------
# autofix_sandbox
# ---------------------------------------------------------------------------


class TestAutofixSandbox:
    def test_formats_python_files(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Badly formatted python
        (tmp_path / "bad.py").write_text("x=1\ny=   2\n")
        autofix_sandbox(tmp_path)
        content = (tmp_path / "bad.py").read_text()
        assert "x = 1" in content

    def test_no_python_files_noop(self, tmp_path: Path):
        _init_repo(tmp_path)
        # No .py files — should not crash
        autofix_sandbox(tmp_path)


# ---------------------------------------------------------------------------
# validate_sandbox
# ---------------------------------------------------------------------------


class TestValidateSandbox:
    def test_pass_clean_python(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed

    def test_pass_no_python_changes(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        (tmp_path / "notes.txt").write_text("hello\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add notes")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed

    def test_fail_lint_error(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        # Undefined variable — ruff will flag
        (tmp_path / "bad.py").write_text("print(undefined_var)\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add bad.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert not result.passed
        assert "Lint failure" in (result.error or "")

    def test_fail_format_error(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        # Valid but badly formatted — ruff format --check will fail
        (tmp_path / "ugly.py").write_text("x=1;y=2\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add ugly.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        # May fail on lint or format depending on ruff config
        assert not result.passed
