"""CLI tests for dgov pane subcommands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.executor import PaneFinalizeResult

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
        with patch("dgov.executor.run_close_only") as mock_close:
            mock_close.return_value = MagicMock(
                slug="nonexistent", closed=False, error="Failed to close pane: nonexistent"
            )
            result = runner.invoke(cli, ["pane", "close", "nonexistent"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"not_found": "nonexistent"}

    def test_close_existing_pane(self, runner: CliRunner) -> None:
        with patch("dgov.executor.run_close_only") as mock_close:
            mock_close.return_value = MagicMock(slug="fix-parser", closed=True, error=None)
            result = runner.invoke(cli, ["pane", "close", "fix-parser"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {"closed": "fix-parser"}


class TestPaneReview:
    def test_review_shows_verdict(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(
                review={
                    "slug": "fix-parser",
                    "verdict": "safe",
                    "commit_count": 3,
                    "files_changed": 2,
                    "diff_stat": "src/parser.py | 10 +++---",
                }
            ),
        ):
            result = runner.invoke(cli, ["pane", "review", "fix-parser"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["verdict"] == "safe"
        assert output["commit_count"] == 3

    def test_review_error_exits_nonzero(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(review={"error": "Pane not found: missing"}),
        ):
            result = runner.invoke(cli, ["pane", "review", "missing"])

        assert result.exit_code == 1
        assert "error" in json.loads(result.output)


class TestPaneLand:
    def test_land_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_finalize_panes",
            return_value=[
                PaneFinalizeResult(
                    slug="task",
                    review={"slug": "task", "verdict": "safe", "commit_count": 2},
                    merge_result={"merged": "task", "branch": "task", "files_changed": 1},
                    error=None,
                    cleanup_error=None,
                )
            ],
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
            ["task"],
            session_root=None,
            resolve="skip",
            squash=True,
            rebase=False,
            close=True,
        )

    def test_land_no_commits_exits(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_finalize_panes",
            return_value=[
                PaneFinalizeResult(
                    slug="task",
                    review={"slug": "task", "verdict": "safe", "commit_count": 0},
                    merge_result=None,
                    error="No commits to merge",
                    cleanup_error=None,
                )
            ],
        ):
            result = runner.invoke(cli, ["pane", "land", "task"])

        assert result.exit_code == 1

    def test_land_review_verdict_blocks_merge(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_finalize_panes",
            return_value=[
                PaneFinalizeResult(
                    slug="task",
                    review={"slug": "task", "verdict": "review", "commit_count": 2},
                    merge_result=None,
                    error="Review verdict is review; refusing to merge",
                    cleanup_error=None,
                )
            ],
        ):
            result = runner.invoke(cli, ["pane", "land", "task"])

        assert result.exit_code == 1
        assert json.loads(result.output.splitlines()[-1]) == {
            "error": "Review verdict is review; refusing to merge"
        }


class TestPaneCreateSlugWarning:
    """Tests for slug deduplication warning."""

    def test_slug_rename_warning_shown(self, runner: CliRunner) -> None:
        """When user-provided slug differs from pane_obj.slug, warn on stderr."""
        with patch.dict("os.environ", {"DGOV_SKIP_GOVERNOR_CHECK": "1"}):
            registry = {"qwen-35b": {"prompt_command": "claude"}}
            with patch("dgov.agents.load_registry", return_value=registry):
                with patch("dgov.context_packet.build_context_packet"):
                    with patch("dgov.executor.run_dispatch_only") as mock_dispatch:
                        mock_dispatch.return_value = MagicMock(
                            slug="fix-parser-1",  # Auto-deduplicated slug
                            pane_id="001",
                            agent="qwen-35b",
                            worktree_path="/tmp/worktree",
                            branch_name="fix-parser-1",
                        )
                        result = runner.invoke(
                            cli,
                            [
                                "pane",
                                "create",
                                "-a",
                                "qwen-35b",
                                "-s",
                                "fix-parser",  # User requested this slug
                                "-p",
                                "Fix the parser",
                                "--no-preflight",
                            ],
                        )

        assert result.exit_code == 0
        # Check that warning was printed to stderr
        assert "Warning: slug fix-parser already in use, renamed to fix-parser-1" in result.stderr

    def test_no_warning_when_slug_unchanged(self, runner: CliRunner) -> None:
        """No warning when user-provided slug matches pane_obj.slug."""
        with patch.dict("os.environ", {"DGOV_SKIP_GOVERNOR_CHECK": "1"}):
            registry = {"qwen-35b": {"prompt_command": "claude"}}
            with patch("dgov.agents.load_registry", return_value=registry):
                with patch("dgov.context_packet.build_context_packet"):
                    with patch("dgov.executor.run_dispatch_only") as mock_dispatch:
                        mock_dispatch.return_value = MagicMock(
                            slug="fix-parser",  # Same as user provided
                            pane_id="001",
                            agent="qwen-35b",
                            worktree_path="/tmp/worktree",
                            branch_name="fix-parser",
                        )
                        result = runner.invoke(
                            cli,
                            [
                                "pane",
                                "create",
                                "-a",
                                "qwen-35b",
                                "-s",
                                "fix-parser",
                                "-p",
                                "Fix the parser",
                                "--no-preflight",
                            ],
                        )

        assert result.exit_code == 0
        # No warning should be shown
        assert "Warning:" not in result.stderr


class TestPaneCreateUnknownAgentFiltering:
    """Tests for filtering physical agent names from error messages."""

    def test_physical_agent_names_filtered_from_error(self, runner: CliRunner) -> None:
        """Error message should only show logical routing names + non-routable registry agents."""
        # Mock registry with both logical and physical names
        registry = {
            "qwen-35b": {},  # Logical name
            "river-35b": {},  # Physical backend (should be filtered)
            "pi": {},  # Non-routable registry agent (should be shown)
        }

        routing_tables = {"qwen-35b": ["river-35b"]}
        with patch("dgov.agents.load_registry", return_value=registry):
            with patch("dgov.router.is_routable", return_value=False):
                with patch("dgov.router.available_names", return_value=["qwen-35b"]):
                    with patch("dgov.router._load_routing_tables", return_value=routing_tables):
                        result = runner.invoke(
                            cli,
                            [
                                "pane",
                                "create",
                                "-a",
                                "unknown-agent",
                                "-p",
                                "Test task",
                            ],
                        )

        assert result.exit_code == 1
        output = result.stderr
        # Should show logical name qwen-35b and non-routable pi
        assert "qwen-35b" in output
        assert "pi" in output
        # Should NOT show physical backend river-35b
        assert "river-35b" not in output
