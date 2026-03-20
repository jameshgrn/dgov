"""CLI tests for dgov pane subcommands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


class TestPaneList:
    def test_empty_list(self, runner: CliRunner) -> None:
        with patch("dgov.status.list_worker_panes", return_value=[]):
            result = runner.invoke(cli, ["pane", "list"])

        assert result.exit_code == 0
        assert "No panes." in result.output

    def test_list_with_panes_shows_slugs(self, runner: CliRunner) -> None:
        panes = [
            {
                "slug": "fix-parser",
                "agent": "pi",
                "state": "active",
                "phase": "running",
                "duration_s": 120,
                "summary": "Fixing parser",
            },
            {
                "slug": "add-tests",
                "agent": "claude",
                "state": "done",
                "phase": "complete",
                "duration_s": 300,
                "summary": "Adding tests",
            },
        ]
        with patch("dgov.status.list_worker_panes", return_value=panes):
            result = runner.invoke(cli, ["pane", "list"])

        assert result.exit_code == 0
        assert "fix-parser" in result.output
        assert "add-tests" in result.output
        assert "pi" in result.output
        assert "claude" in result.output

    def test_list_json_output(self, runner: CliRunner) -> None:
        panes = [{"slug": "task", "agent": "pi"}]
        with patch("dgov.status.list_worker_panes", return_value=panes):
            result = runner.invoke(cli, ["pane", "list", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == panes


class TestPaneClose:
    def test_close_unknown_slug(self, runner: CliRunner) -> None:
        with patch("dgov.lifecycle.close_worker_pane", return_value=False):
            result = runner.invoke(cli, ["pane", "close", "nonexistent"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {"already_closed": "nonexistent"}

    def test_close_existing_pane(self, runner: CliRunner) -> None:
        with patch("dgov.lifecycle.close_worker_pane", return_value=True):
            result = runner.invoke(cli, ["pane", "close", "fix-parser"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {"closed": "fix-parser"}


class TestPaneReview:
    def test_review_shows_verdict(self, runner: CliRunner) -> None:
        review_data = {
            "slug": "fix-parser",
            "verdict": "safe",
            "commit_count": 3,
            "files_changed": 2,
            "diff_stat": "src/parser.py | 10 +++---",
        }
        with patch("dgov.inspection.review_worker_pane", return_value=review_data):
            result = runner.invoke(cli, ["pane", "review", "fix-parser"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["verdict"] == "safe"
        assert output["commit_count"] == 3

    def test_review_error_exits_nonzero(self, runner: CliRunner) -> None:
        with patch(
            "dgov.inspection.review_worker_pane",
            return_value={"error": "Pane not found: missing"},
        ):
            result = runner.invoke(cli, ["pane", "review", "missing"])

        assert result.exit_code == 1
        assert "error" in json.loads(result.output)


class TestPaneLand:
    def test_land_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_land_only",
            return_value=MagicMock(
                review={"slug": "task", "verdict": "safe", "commit_count": 2},
                merge_result={"merged": "task", "branch": "task", "files_changed": 1},
                error=None,
            ),
        ) as mock_land:
            result = runner.invoke(cli, ["pane", "land", "task"])

        assert result.exit_code == 0
        # land emits review line then indented merge JSON — split at first newline of merge block
        output = result.output.strip()
        first_nl = output.index("\n")
        review_line = json.loads(output[:first_nl])
        merge_line = json.loads(output[first_nl + 1 :])
        assert review_line["review"] == "safe"
        assert review_line["commits"] == 2
        assert merge_line["merged"] == "task"
        mock_land.assert_called_once_with(
            ".",
            "task",
            session_root=None,
            resolve="skip",
            squash=True,
            rebase=False,
        )

    def test_land_no_commits_exits(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_land_only",
            return_value=MagicMock(
                review={"slug": "task", "verdict": "safe", "commit_count": 0},
                merge_result=None,
                error="No commits to merge",
            ),
        ):
            result = runner.invoke(cli, ["pane", "land", "task"])

        assert result.exit_code == 1

    def test_land_review_verdict_blocks_merge(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_land_only",
            return_value=MagicMock(
                review={"slug": "task", "verdict": "review", "commit_count": 2},
                merge_result=None,
                error="Review verdict is review; refusing to merge",
            ),
        ):
            result = runner.invoke(cli, ["pane", "land", "task"])

        assert result.exit_code == 1
        assert json.loads(result.output.splitlines()[-1]) == {
            "error": "Review verdict is review; refusing to merge"
        }


class TestPaneMerge:
    def test_merge_uses_canonical_merge_only(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_merge_only",
            return_value=MagicMock(
                merge_result={"merged": "task", "branch": "task"},
            ),
        ) as mock_merge:
            result = runner.invoke(cli, ["pane", "merge", "task", "--resolve", "manual"])

        assert result.exit_code == 0
        mock_merge.assert_called_once_with(
            ".",
            "task",
            session_root=None,
            resolve="manual",
            squash=True,
            rebase=False,
        )

    def test_merge_error_exits_nonzero(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_merge_only",
            return_value=MagicMock(merge_result={"error": "conflicts"}),
        ):
            result = runner.invoke(cli, ["pane", "merge", "task"])

        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "conflicts"
