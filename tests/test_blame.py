"""Tests for dgov/blame module."""

from unittest.mock import MagicMock, patch

import pytest

from dgov.blame import blame_file


@pytest.mark.unit
def test_blame_file_returns_expected_structure():
    """Test that blame_file returns the expected dict structure."""
    mock_events = [
        {
            "event": "pane_created",
            "pane": "test-pane-1",
            "agent": "qwen-35b",
            "prompt": "Test prompt",
            "ts": "2026-03-21T00:00:00Z",
        },
        {
            "event": "pane_merged",
            "pane": "test-pane-1",
            "merge_sha": "abc123def456",
            "ts": "2026-03-21T01:00:00Z",
        },
    ]

    mock_git_output = """COMMIT:abc123d Merge branch 'test-pane-1'
 src/dgov/test.py"""

    with patch("dgov.blame.read_events", return_value=mock_events):
        with patch("dgov.blame.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = mock_git_output
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = blame_file(
                project_root="/tmp/test", file_path="src/dgov/test.py", session_root="/tmp/test"
            )

    assert isinstance(result, dict)
    assert "file" in result
    assert "history" in result
    assert result["file"] == "src/dgov/test.py"
    assert isinstance(result["history"], list)
    assert len(result["history"]) > 0
    history_entry = result["history"][0]
    assert "commit" in history_entry
    assert "subject" in history_entry
    assert "slug" in history_entry
    assert "agent" in history_entry
    assert "prompt" in history_entry
    assert "merged_at" in history_entry
    assert "files_in_change" in history_entry
