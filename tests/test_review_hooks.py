"""Tests for user-defined review hooks."""

from __future__ import annotations

import subprocess

import pytest

from dgov.settlement import review_sandbox


@pytest.fixture
def mock_git_worktree(tmp_path):
    """Create a directory that looks like a git worktree with changes."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    subprocess.run(["git", "init"], cwd=wt, check=True)
    (wt / "file.py").write_text("import os\nprint('hello')")
    # No need to commit, review_sandbox uses git status --porcelain
    return wt


@pytest.mark.unit
def test_review_hook_fail(mock_git_worktree, tmp_path, monkeypatch):
    """Review should fail if a hook returns non-zero."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    dgov_dir = project_root / ".dgov"
    dgov_dir.mkdir()

    # Define a hook that fails if 'import os' is found
    # Using 'grep -q' which exits 0 if found, so we invert it with '!' or similar
    # or just use 'grep' and check logic.
    # Hook: "grep -qv 'import os' {file}" -> fails if 'import os' is present
    # Better hook for test: "! grep -q 'import os' {file}"
    config_toml = """
[project]
review_hooks = [
    "! grep -q 'import os' {file}"
]
"""
    (dgov_dir / "project.toml").write_text(config_toml)

    # Run review
    result = review_sandbox(mock_git_worktree, project_root=str(project_root))

    assert result.passed is False
    assert result.verdict == "hook_fail"
    assert result.error is not None
    assert "Review hook failed" in result.error
    assert "! grep -q 'import os'" in result.error


@pytest.mark.unit
def test_review_hook_pass(mock_git_worktree, tmp_path, monkeypatch):
    """Review should pass if all hooks return zero."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    dgov_dir = project_root / ".dgov"
    dgov_dir.mkdir()

    config_toml = """
[project]
review_hooks = [
    "grep -q 'import os' {file}"
]
"""
    (dgov_dir / "project.toml").write_text(config_toml)

    # Run review
    result = review_sandbox(mock_git_worktree, project_root=str(project_root))

    assert result.passed is True
    assert result.verdict == "ok"


@pytest.mark.unit
def test_review_hook_binary_detection(mock_git_worktree, tmp_path):
    """Review should fail if a binary file is added."""
    project_root = tmp_path / "project_bin"
    project_root.mkdir()
    (project_root / ".dgov").mkdir()
    (project_root / ".dgov" / "project.toml").write_text("""
[project]
review_hooks = [
    "! file --mime {files} | grep -q 'charset=binary'"
]
""")

    # Create a binary file (contains null byte)
    (mock_git_worktree / "binary.bin").write_bytes(b"\x00\xff\x00\xff")

    result = review_sandbox(mock_git_worktree, project_root=str(project_root))
    assert result.passed is False
    assert result.verdict == "hook_fail"


@pytest.mark.unit
def test_review_hook_secret_leakage(mock_git_worktree, tmp_path):
    """Review should fail if a secret/key is found."""
    project_root = tmp_path / "project_secret"
    project_root.mkdir()
    (project_root / ".dgov").mkdir()
    (project_root / ".dgov" / "project.toml").write_text("""
[project]
review_hooks = [
    "! grep -rE '(sk-[a-zA-Z0-9]{24,}|AI_[A-Z0-9_]{24,})' {files}"
]
""")

    # Add file with secret (24 chars after sk-)
    (mock_git_worktree / "secret.env").write_text("OPENAI_KEY = sk-abcdefghijklmnopqrstuvwx")

    result = review_sandbox(mock_git_worktree, project_root=str(project_root))
    assert result.passed is False
    assert result.verdict == "hook_fail"
