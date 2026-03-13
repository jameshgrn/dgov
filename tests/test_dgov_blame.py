"""Unit tests for dgov.blame — file attribution via event journal + git history."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.blame import _extract_slug_from_subject, _load_events, blame_file

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _extract_slug_from_subject
# ---------------------------------------------------------------------------


class TestExtractSlugFromSubject:
    def test_merge_branch_pattern(self) -> None:
        assert _extract_slug_from_subject("Merge branch 'fix-lint'") == "fix-lint"

    def test_merge_simple_pattern(self) -> None:
        assert _extract_slug_from_subject("Merge fix-lint") == "fix-lint"

    def test_no_match(self) -> None:
        assert _extract_slug_from_subject("Fix the lint errors") is None

    def test_merge_branch_with_slashes(self) -> None:
        assert (
            _extract_slug_from_subject("Merge branch 'feature/add-thing'") == "feature/add-thing"
        )


# ---------------------------------------------------------------------------
# _load_events
# ---------------------------------------------------------------------------


class TestLoadEvents:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _load_events(str(tmp_path)) == []

    def test_reads_jsonl(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir()
        events_file = events_dir / "events.jsonl"
        events_file.write_text(
            json.dumps({"event": "pane_created", "pane": "fix-bug"})
            + "\n"
            + json.dumps({"event": "pane_merged", "pane": "fix-bug"})
            + "\n"
        )
        events = _load_events(str(tmp_path))
        assert len(events) == 2
        assert events[0]["event"] == "pane_created"
        assert events[1]["event"] == "pane_merged"

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir()
        events_file = events_dir / "events.jsonl"
        events_file.write_text(
            "not valid json\n" + json.dumps({"event": "pane_created", "pane": "ok"}) + "\n" + "\n"
        )
        events = _load_events(str(tmp_path))
        assert len(events) == 1
        assert events[0]["pane"] == "ok"


# ---------------------------------------------------------------------------
# blame_file — SHA-to-slug resolution
# ---------------------------------------------------------------------------


class TestBlameFileShaResolution:
    def _make_events(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "event": "pane_created",
                "pane": "fix-lint",
                "agent": "pi",
                "prompt": "Fix lint errors",
                "ts": "2026-01-01T00:00:00Z",
            },
            {
                "event": "pane_merged",
                "pane": "fix-lint",
                "merge_sha": "abc1234567890def",
                "ts": "2026-01-01T01:00:00Z",
            },
            {
                "event": "pane_created",
                "pane": "add-tests",
                "agent": "claude",
                "prompt": "Add unit tests",
                "ts": "2026-01-02T00:00:00Z",
            },
            {
                "event": "pane_merged",
                "pane": "add-tests",
                "merge_sha": "def5678901234abc",
                "ts": "2026-01-02T01:00:00Z",
            },
        ]
        events_file = events_dir / "events.jsonl"
        events_file.write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_sha_matches_full_hash(self, tmp_path: Path) -> None:
        self._make_events(tmp_path)

        git_output = "COMMIT:abc1234567890def Fix lint errors\nsrc/foo.py\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/foo.py", str(tmp_path))

        assert len(result["history"]) == 1
        assert result["history"][0]["slug"] == "fix-lint"
        assert result["history"][0]["agent"] == "pi"
        assert result["history"][0]["merged_at"] == "2026-01-01T01:00:00Z"

    def test_sha_matches_short_hash(self, tmp_path: Path) -> None:
        self._make_events(tmp_path)

        # Git returns short hash that matches first 7 chars of merge_sha
        git_output = "COMMIT:abc1234 Fix lint errors\nsrc/foo.py\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/foo.py", str(tmp_path))

        assert len(result["history"]) == 1
        assert result["history"][0]["slug"] == "fix-lint"


# ---------------------------------------------------------------------------
# blame_file — subject line slug extraction fallback
# ---------------------------------------------------------------------------


class TestBlameFileSubjectFallback:
    def test_merge_branch_subject_extracts_slug(self, tmp_path: Path) -> None:
        # No events — fallback to subject parsing
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        git_output = "COMMIT:deadbeef1234567 Merge branch 'fix-typo'\nsrc/bar.py\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/bar.py", str(tmp_path))

        assert len(result["history"]) == 1
        assert result["history"][0]["slug"] == "fix-typo"
        # No event data — agent/prompt should be empty
        assert result["history"][0]["agent"] == ""
        assert result["history"][0]["prompt"] == ""

    def test_no_slug_from_subject(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        git_output = "COMMIT:1111111 Initial commit\nREADME.md\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "README.md", str(tmp_path))

        assert len(result["history"]) == 1
        assert result["history"][0]["slug"] == ""
        assert result["history"][0]["agent"] == ""


# ---------------------------------------------------------------------------
# blame_file — agent_filter
# ---------------------------------------------------------------------------


class TestBlameFileAgentFilter:
    def _setup(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "pane_created", "pane": "pi-task", "agent": "pi", "prompt": "P1"},
            {"event": "pane_merged", "pane": "pi-task", "merge_sha": "aaaa111"},
            {"event": "pane_created", "pane": "claude-task", "agent": "claude", "prompt": "C1"},
            {"event": "pane_merged", "pane": "claude-task", "merge_sha": "bbbb222"},
        ]
        (events_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_filters_to_matching_agent(self, tmp_path: Path) -> None:
        self._setup(tmp_path)

        git_output = (
            "COMMIT:aaaa111 Fix from pi\nsrc/foo.py\nCOMMIT:bbbb222 Fix from claude\nsrc/foo.py\n"
        )
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(
                str(tmp_path),
                "src/foo.py",
                str(tmp_path),
                last_only=False,
                agent_filter="claude",
            )

        assert len(result["history"]) == 1
        assert result["history"][0]["agent"] == "claude"

    def test_filter_no_match_returns_empty(self, tmp_path: Path) -> None:
        self._setup(tmp_path)

        git_output = "COMMIT:aaaa111 Fix from pi\nsrc/foo.py\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(
                str(tmp_path),
                "src/foo.py",
                str(tmp_path),
                last_only=False,
                agent_filter="gemini",
            )

        assert result["history"] == []


# ---------------------------------------------------------------------------
# blame_file — last_only
# ---------------------------------------------------------------------------


class TestBlameFileLastOnly:
    def test_last_only_true_returns_one(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "pane_created", "pane": "t1", "agent": "pi", "prompt": "P1"},
            {"event": "pane_merged", "pane": "t1", "merge_sha": "aaa1111"},
            {"event": "pane_created", "pane": "t2", "agent": "claude", "prompt": "C1"},
            {"event": "pane_merged", "pane": "t2", "merge_sha": "bbb2222"},
        ]
        (events_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

        git_output = (
            "COMMIT:aaa1111 First change\nsrc/x.py\nCOMMIT:bbb2222 Second change\nsrc/x.py\n"
        )
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/x.py", str(tmp_path), last_only=True)

        assert len(result["history"]) == 1
        assert result["history"][0]["commit"] == "aaa1111"

    def test_last_only_false_returns_all(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "pane_created", "pane": "t1", "agent": "pi", "prompt": "P1"},
            {"event": "pane_merged", "pane": "t1", "merge_sha": "aaa1111"},
            {"event": "pane_created", "pane": "t2", "agent": "claude", "prompt": "C1"},
            {"event": "pane_merged", "pane": "t2", "merge_sha": "bbb2222"},
        ]
        (events_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

        git_output = (
            "COMMIT:aaa1111 First change\nsrc/x.py\nCOMMIT:bbb2222 Second change\nsrc/x.py\n"
        )
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/x.py", str(tmp_path), last_only=False)

        assert len(result["history"]) == 2


# ---------------------------------------------------------------------------
# blame_file — empty history (file not in git)
# ---------------------------------------------------------------------------


class TestBlameFileEmpty:
    def test_git_log_failure_returns_error(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        mock_result = MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "nope.py", str(tmp_path))

        assert result["history"] == []
        assert "error" in result
        assert "not a git repository" in result["error"]

    def test_no_commits_touching_file(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "new-file.py", str(tmp_path))

        assert result["history"] == []
        assert "error" not in result


# ---------------------------------------------------------------------------
# blame_file — files_in_change count
# ---------------------------------------------------------------------------


class TestBlameFileFilesInChange:
    def test_counts_files_per_commit(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        git_output = "COMMIT:aaa1111 Big refactor\nsrc/a.py\nsrc/b.py\nsrc/c.py\n"
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/a.py", str(tmp_path), last_only=False)

        assert len(result["history"]) == 1
        assert result["history"][0]["files_in_change"] == 3


# ---------------------------------------------------------------------------
# blame_file — multiple commits, mixed resolution
# ---------------------------------------------------------------------------


class TestBlameFileMixedResolution:
    def test_sha_resolution_then_subject_fallback(self, tmp_path: Path) -> None:
        """First commit resolved via SHA lookup, second via subject line."""
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "pane_created", "pane": "known-slug", "agent": "pi", "prompt": "Fix it"},
            {"event": "pane_merged", "pane": "known-slug", "merge_sha": "sha1234"},
        ]
        (events_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

        git_output = (
            "COMMIT:sha1234 Fix something\nsrc/f.py\n"
            "COMMIT:unknown Merge branch 'old-branch'\nsrc/f.py\n"
        )
        mock_result = MagicMock(returncode=0, stdout=git_output, stderr="")

        with patch("dgov.blame.subprocess.run", return_value=mock_result):
            result = blame_file(str(tmp_path), "src/f.py", str(tmp_path), last_only=False)

        assert len(result["history"]) == 2
        assert result["history"][0]["slug"] == "known-slug"
        assert result["history"][0]["agent"] == "pi"
        assert result["history"][1]["slug"] == "old-branch"
        assert result["history"][1]["agent"] == ""
