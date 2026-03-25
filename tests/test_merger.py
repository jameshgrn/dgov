"""Unit tests for post-merge validation including conflict marker detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.merger import _check_conflict_markers, _lint_fix_merged_files

pytestmark = pytest.mark.unit


def test_lint_file_count_distinct(tmp_path: Path) -> None:
    """Test that lint file count extracts distinct filenames from ruff output.

    Ruff output can have multiple error lines for the same file. This test verifies
    that we count unique files, not raw error lines.
    """
    # Create a fake Python file
    test_file = tmp_path / "example.py"
    test_file.write_text("x=1\ny = 2\nz  =3\n")

    # Mock subprocess.run to return ruff output with multiple errors for same file
    mock_result = MagicMock()
    mock_result.returncode = 1  # ruff found issues
    # Use relative paths in ruff output - function will resolve them from abs_files
    mock_result.stdout = """example.py:1:1: E741 ambiguous variable name 'x'
example.py:2:5: E221 multiple spaces before operator
example.py:3:4: E221 multiple spaces before operator
other_file.py:10:1: F841 local variable 'unused' is assigned to but never used"""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _lint_fix_merged_files(str(tmp_path), ["example.py", "other_file.py"])

    # Verify that distinct file count is 2, not raw line count (4)
    assert "lint_unfixable" in result
    assert "lint_unfixable_files" in result
    # Should have 2 distinct files, not 4 lines
    assert len(result["lint_unfixable"]) == 4  # 4 error lines
    assert len(result["lint_unfixable_files"]) == 2  # 2 distinct files
    # Verify the file list is correct (ruff output paths are relative)
    assert set(result["lint_unfixable_files"]) == {"example.py", "other_file.py"}


def test_lint_warns_not_fails(tmp_path: Path) -> None:
    """Test that unfixable lint issues produce a warning, not a merge failure.

    After merge has landed, lint is advisory. We should emit logger.warning()
    and still consider the merge successful (validation_failed should remain False).
    """
    # Create a fake Python file
    test_file = tmp_path / "example.py"
    test_file.write_text("x=1\n")

    # Mock subprocess.run to return ruff output with unfixable issues
    mock_result = MagicMock()
    mock_result.returncode = 1  # ruff found issues that can't be auto-fixed
    mock_result.stdout = """example.py:1:1: E741 ambiguous variable name 'x'"""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _lint_fix_merged_files(str(tmp_path), ["example.py"])

    # Verify lint issues are captured but don't affect validation status
    assert "lint_unfixable" in result
    assert "lint_unfixable_files" in result
    assert len(result["lint_unfixable"]) == 1
    assert len(result["lint_unfixable_files"]) == 1

    # The merge itself remains successful - lint issues are advisory
    # (validation_failed logic is outside this function, but the function should not
    # set any flag that would cause validation to fail)
    assert "validation_failed" not in result


def test_lint_no_issues(tmp_path: Path) -> None:
    """Test that clean ruff output returns empty dict."""
    test_file = tmp_path / "clean.py"
    test_file.write_text("x = 1\n")

    mock_result = MagicMock()
    mock_result.returncode = 0  # no issues found
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _lint_fix_merged_files(str(tmp_path), ["clean.py"])

    assert result == {}


def test_lint_no_python_files(tmp_path: Path) -> None:
    """Test that non-Python files are ignored."""
    txt_file = tmp_path / "readme.txt"
    txt_file.write_text("hello")

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "error"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _lint_fix_merged_files(str(tmp_path), ["readme.txt"])

    # No Python files, should return early
    assert result == {}


def test_lint_same_file_multiple_errors(tmp_path: Path) -> None:
    """Test that multiple errors on same file count as one file."""
    test_file = tmp_path / "buggy.py"
    test_file.write_text("a=1\nb=2\nc=3\nd=4\ne=5\n")

    mock_result = MagicMock()
    mock_result.returncode = 1
    # 5 errors, all on the same file
    mock_result.stdout = """buggy.py:1:1: E741 ambiguous variable name 'a'
buggy.py:2:1: E741 ambiguous variable name 'b'
buggy.py:3:1: E741 ambiguous variable name 'c'
buggy.py:4:1: E741 ambiguous variable name 'd'
buggy.py:5:1: E741 ambiguous variable name 'e'"""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _lint_fix_merged_files(str(tmp_path), ["buggy.py"])

    # 5 error lines, but only 1 distinct file
    assert len(result["lint_unfixable"]) == 5
    assert len(result["lint_unfixable_files"]) == 1
    assert result["lint_unfixable_files"] == ["buggy.py"]


@pytest.mark.unit
def test_conflict_marker_detection_finds_markers(tmp_path: Path) -> None:
    """Test that conflict markers are detected in merged files."""
    clean = tmp_path / "clean.py"
    clean.write_text("def hello(): pass\n")

    dirty = tmp_path / "dirty.py"
    dirty.write_text("def hello():\n<<<<<<< HEAD\n    pass\n=======\n    return\n>>>>>>> branch\n")

    result = _check_conflict_markers(str(tmp_path), ["clean.py", "dirty.py"])
    assert result == ["dirty.py"]


@pytest.mark.unit
def test_conflict_marker_detection_clean_files(tmp_path: Path) -> None:
    """Test that clean files return empty list."""
    clean = tmp_path / "ok.py"
    clean.write_text("x = 1\n")

    result = _check_conflict_markers(str(tmp_path), ["ok.py"])
    assert result == []


@pytest.mark.unit
def test_conflict_marker_detection_missing_file(tmp_path: Path) -> None:
    """Test that missing files are skipped gracefully."""
    result = _check_conflict_markers(str(tmp_path), ["nonexistent.py"])
    assert result == []
