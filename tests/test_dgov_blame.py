"""Unit tests for dgov.blame — file attribution via event journal + git history."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.blame import (
    _extract_slug_from_subject,
    _group_blame_lines,
    _parse_porcelain_blame,
    blame_file,
    blame_lines,
)
from dgov.persistence import read_events

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
# read_events (centralized in persistence.py)
# ---------------------------------------------------------------------------


class TestReadEvents:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_events(str(tmp_path)) == []

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
        events = read_events(str(tmp_path))
        assert len(events) == 2
        assert events[0]["event"] == "pane_created"
        assert events[1]["event"] == "pane_merged"

    def test_warns_on_malformed_lines(self, tmp_path: Path, caplog) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir()
        events_file = events_dir / "events.jsonl"
        events_file.write_text(
            "not valid json\n" + json.dumps({"event": "pane_created", "pane": "ok"}) + "\n" + "\n"
        )
        events = read_events(str(tmp_path))
        assert len(events) == 1
        assert events[0]["pane"] == "ok"
        assert "Malformed JSON" in caplog.text

    def test_filters_by_slug(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir()
        events_file = events_dir / "events.jsonl"
        events_file.write_text(
            json.dumps({"event": "pane_created", "pane": "a"})
            + "\n"
            + json.dumps({"event": "pane_created", "pane": "b"})
            + "\n"
        )
        events = read_events(str(tmp_path), slug="a")
        assert len(events) == 1
        assert events[0]["pane"] == "a"


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


# ---------------------------------------------------------------------------
# _parse_porcelain_blame
# ---------------------------------------------------------------------------

PORCELAIN_SAMPLE = (
    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 1 1 3\n"
    "author Alice\n"
    "author-mail <alice@example.com>\n"
    "author-time 1700000000\n"
    "author-tz +0000\n"
    "committer Alice\n"
    "committer-mail <alice@example.com>\n"
    "committer-time 1700000000\n"
    "committer-tz +0000\n"
    "summary Initial commit\n"
    "filename src/foo.py\n"
    "\tdef hello():\n"
    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 1 2\n"
    "\t    return 'hi'\n"
    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 1 3\n"
    "\t\n"
    "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b200 4 4 1\n"
    "author Bob\n"
    "author-mail <bob@example.com>\n"
    "author-time 1700001000\n"
    "author-tz +0000\n"
    "committer Bob\n"
    "committer-mail <bob@example.com>\n"
    "committer-time 1700001000\n"
    "committer-tz +0000\n"
    "summary Add greeting\n"
    "filename src/foo.py\n"
    "\tprint('hello')\n"
)


class TestParsePorcelainBlame:
    def test_parses_entries(self) -> None:
        entries = _parse_porcelain_blame(PORCELAIN_SAMPLE)
        assert len(entries) == 4

    def test_line_numbers(self) -> None:
        entries = _parse_porcelain_blame(PORCELAIN_SAMPLE)
        assert [e["line_no"] for e in entries] == [1, 2, 3, 4]

    def test_authors(self) -> None:
        entries = _parse_porcelain_blame(PORCELAIN_SAMPLE)
        assert entries[0]["author"] == "Alice"
        assert entries[1]["author"] == "Alice"
        assert entries[3]["author"] == "Bob"

    def test_content(self) -> None:
        entries = _parse_porcelain_blame(PORCELAIN_SAMPLE)
        assert entries[0]["content"] == "def hello():"
        assert entries[1]["content"] == "    return 'hi'"
        assert entries[3]["content"] == "print('hello')"

    def test_short_sha(self) -> None:
        entries = _parse_porcelain_blame(PORCELAIN_SAMPLE)
        assert entries[0]["commit"] == "a1b2c3d"
        assert entries[3]["commit"] == "b2c3d4e"

    def test_empty_output(self) -> None:
        assert _parse_porcelain_blame("") == []


# ---------------------------------------------------------------------------
# _group_blame_lines
# ---------------------------------------------------------------------------


class TestGroupBlameLines:
    def test_groups_consecutive_same_author(self) -> None:
        entries = [
            {
                "line_no": 1,
                "commit": "abc1234",
                "author": "A",
                "content": "x",
                "slug": "",
                "agent": "",
            },
            {
                "line_no": 2,
                "commit": "abc1234",
                "author": "A",
                "content": "y",
                "slug": "",
                "agent": "",
            },
            {
                "line_no": 3,
                "commit": "abc1234",
                "author": "A",
                "content": "z",
                "slug": "",
                "agent": "",
            },
        ]
        groups = _group_blame_lines(entries)
        assert len(groups) == 1
        assert groups[0]["line_start"] == 1
        assert groups[0]["line_end"] == 3
        assert groups[0]["content"] == ["x", "y", "z"]

    def test_splits_on_different_commit(self) -> None:
        entries = [
            {
                "line_no": 1,
                "commit": "aaa1111",
                "author": "A",
                "content": "a",
                "slug": "",
                "agent": "",
            },
            {
                "line_no": 2,
                "commit": "bbb2222",
                "author": "A",
                "content": "b",
                "slug": "",
                "agent": "",
            },
        ]
        groups = _group_blame_lines(entries)
        assert len(groups) == 2

    def test_splits_on_different_agent(self) -> None:
        entries = [
            {
                "line_no": 1,
                "commit": "aaa1111",
                "author": "A",
                "content": "a",
                "slug": "s1",
                "agent": "pi",
            },
            {
                "line_no": 2,
                "commit": "aaa1111",
                "author": "A",
                "content": "b",
                "slug": "s2",
                "agent": "claude",
            },
        ]
        groups = _group_blame_lines(entries)
        assert len(groups) == 2

    def test_splits_on_non_consecutive_lines(self) -> None:
        entries = [
            {
                "line_no": 1,
                "commit": "aaa1111",
                "author": "A",
                "content": "a",
                "slug": "",
                "agent": "",
            },
            {
                "line_no": 5,
                "commit": "aaa1111",
                "author": "A",
                "content": "b",
                "slug": "",
                "agent": "",
            },
        ]
        groups = _group_blame_lines(entries)
        assert len(groups) == 2

    def test_empty_entries(self) -> None:
        assert _group_blame_lines([]) == []


# ---------------------------------------------------------------------------
# blame_lines — integration
# ---------------------------------------------------------------------------


class TestBlameLines:
    def _make_events(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "pane_created", "pane": "fix-lint", "agent": "pi", "prompt": "Fix lint"},
            {
                "event": "pane_merged",
                "pane": "fix-lint",
                "merge_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            },
        ]
        (events_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_attributes_lines_to_agent(self, tmp_path: Path) -> None:
        self._make_events(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("line1\nline2\n")

        blame_mock = MagicMock(returncode=0, stdout=PORCELAIN_SAMPLE, stderr="")
        slug_mock = MagicMock(returncode=0, stdout="", stderr="")

        with patch("dgov.blame.subprocess.run", side_effect=[blame_mock, slug_mock, slug_mock]):
            result = blame_lines(str(tmp_path), "src/foo.py", str(tmp_path))

        assert "lines" in result
        assert len(result["lines"]) > 0
        first = result["lines"][0]
        assert first["slug"] == "fix-lint"
        assert first["agent"] == "pi"

    def test_file_not_found(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")

        result = blame_lines(str(tmp_path), "nope.py", str(tmp_path))
        assert "error" in result
        assert result["lines"] == []

    def test_line_range_filter(self, tmp_path: Path) -> None:
        self._make_events(tmp_path)
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "foo.py").write_text("a\nb\nc\nd\n")

        blame_mock = MagicMock(returncode=0, stdout=PORCELAIN_SAMPLE, stderr="")
        slug_mock = MagicMock(returncode=0, stdout="", stderr="")

        with patch("dgov.blame.subprocess.run", side_effect=[blame_mock, slug_mock]):
            result = blame_lines(
                str(tmp_path),
                "src/foo.py",
                str(tmp_path),
                start_line=2,
                end_line=3,
            )

        for group in result["lines"]:
            assert group["line_start"] >= 2
            assert group["line_end"] <= 3

    def test_agent_filter(self, tmp_path: Path) -> None:
        self._make_events(tmp_path)
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "foo.py").write_text("a\nb\nc\nd\n")

        blame_mock = MagicMock(returncode=0, stdout=PORCELAIN_SAMPLE, stderr="")
        slug_mock = MagicMock(returncode=0, stdout="", stderr="")

        with patch("dgov.blame.subprocess.run", side_effect=[blame_mock, slug_mock, slug_mock]):
            result = blame_lines(
                str(tmp_path),
                "src/foo.py",
                str(tmp_path),
                agent_filter="claude",
            )

        # pi lines filtered out, no claude lines in sample → empty
        for group in result["lines"]:
            assert group["agent"] == "claude"

    def test_fallback_to_author(self, tmp_path: Path) -> None:
        """Lines with no agent attribution should show git author."""
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "foo.py").write_text("a\nb\n")

        blame_mock = MagicMock(returncode=0, stdout=PORCELAIN_SAMPLE, stderr="")
        slug_mock = MagicMock(returncode=0, stdout="", stderr="")

        # 1 blame call + 1 slug lookup per unique SHA (2 unique SHAs in sample)
        with patch("dgov.blame.subprocess.run") as mock_run:
            mock_run.side_effect = [blame_mock] + [slug_mock] * 4
            result = blame_lines(str(tmp_path), "src/foo.py", str(tmp_path))

        assert len(result["lines"]) > 0
        for group in result["lines"]:
            assert group["author"] != ""
            assert group["slug"] == ""
            assert group["agent"] == ""

    def test_git_blame_failure(self, tmp_path: Path) -> None:
        events_dir = tmp_path / ".dgov"
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / "events.jsonl").write_text("")
        (tmp_path / "bad.py").write_text("")

        blame_mock = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("dgov.blame.subprocess.run", return_value=blame_mock):
            result = blame_lines(str(tmp_path), "bad.py", str(tmp_path))

        assert "error" in result
        assert result["lines"] == []


# ---------------------------------------------------------------------------
# CLI --line-level and --lines flags
# ---------------------------------------------------------------------------


class TestBlameCLILineLevel:
    @pytest.fixture(autouse=True)
    def _skip_governor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")

    def test_line_level_flag(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        mock_result = {
            "file": "src/foo.py",
            "lines": [
                {
                    "line_start": 1,
                    "line_end": 3,
                    "commit": "abc1234",
                    "slug": "fix",
                    "agent": "pi",
                    "author": "A",
                    "content": ["a", "b", "c"],
                },
            ],
        }
        with patch("dgov.blame.blame_lines", return_value=mock_result):
            result = runner.invoke(
                cli,
                ["blame", "src/foo.py", "--line-level", "-r", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "lines" in data

    def test_lines_range_flag(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        mock_result = {"file": "src/foo.py", "lines": []}
        with patch("dgov.blame.blame_lines", return_value=mock_result) as mock_fn:
            result = runner.invoke(
                cli,
                ["blame", "src/foo.py", "--lines", "10-20", "-r", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["start_line"] == 10
        assert call_kwargs["end_line"] == 20

    def test_lines_single_line(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        mock_result = {"file": "src/foo.py", "lines": []}
        with patch("dgov.blame.blame_lines", return_value=mock_result) as mock_fn:
            result = runner.invoke(
                cli,
                ["blame", "src/foo.py", "--lines", "5", "-r", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["start_line"] == 5
        assert call_kwargs["end_line"] == 5

    def test_default_uses_blame_file(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        mock_result = {"file": "src/foo.py", "history": []}
        with patch("dgov.blame.blame_file", return_value=mock_result) as mock_fn:
            result = runner.invoke(
                cli,
                ["blame", "src/foo.py", "-r", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        mock_fn.assert_called_once()

    def test_agent_filter_with_line_level(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        mock_result = {"file": "src/foo.py", "lines": []}
        with patch("dgov.blame.blame_lines", return_value=mock_result) as mock_fn:
            result = runner.invoke(
                cli,
                ["blame", "src/foo.py", "--line-level", "--agent", "pi", "-r", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["agent_filter"] == "pi"
