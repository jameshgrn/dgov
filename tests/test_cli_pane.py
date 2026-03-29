"""CLI tests for dgov pane subcommands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.executor import PaneFinalizeResult
from dgov.inspection import ReviewInfo
from dgov.merger import MergeSuccess

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
        expected = {
            "error": "Pane not found: nonexistent",
            "hint": "Run 'dgov pane list -r .' to see active panes",
        }
        assert json.loads(result.output) == expected

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
                review=ReviewInfo(
                    slug="fix-parser",
                    verdict="safe",
                    commit_count=3,
                    files_changed=2,
                    stat="src/parser.py | 10 +++---",
                )
            ),
        ) as mock_review:
            result = runner.invoke(cli, ["pane", "review", "fix-parser"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["verdict"] == "safe"
        assert output["commit_count"] == 3
        assert "tests" not in output
        assert "freshness_info" not in output
        assert mock_review.call_args.kwargs["emit_events"] is False

    def test_review_error_exits_nonzero(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(
                review=ReviewInfo(slug="missing", error="Pane not found: missing")
            ),
        ):
            result = runner.invoke(cli, ["pane", "review", "missing"])

        assert result.exit_code == 1
        assert "error" in json.loads(result.output)

    def test_land_dry_run_stays_read_only(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(review=ReviewInfo(slug="task", verdict="safe", commit_count=2)),
        ) as mock_review:
            result = runner.invoke(cli, ["pane", "land", "task", "--dry-run"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["dry_run"] is True
        assert output["would_merge"] is True
        assert mock_review.call_args.kwargs["emit_events"] is False

    def test_merge_dry_run_stays_read_only(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(
                review=ReviewInfo(slug="task", verdict="safe", commit_count=2, files_changed=1)
            ),
        ) as mock_review:
            result = runner.invoke(cli, ["pane", "merge", "task", "--dry-run"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["dry_run"] is True
        assert output["verdict"] == "safe"
        assert mock_review.call_args.kwargs["emit_events"] is False


class TestPaneLand:
    def test_land_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_finalize_panes",
            return_value=[
                PaneFinalizeResult(
                    slug="task",
                    review=ReviewInfo(slug="task", verdict="safe", commit_count=2),
                    merge_result=MergeSuccess(merged="task", branch="task", files_changed=1),
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


class TestPaneCreatePlanPath:
    """Tests for worker dispatch via plan pipeline."""

    def test_worker_dispatches_via_plan(self, runner: CliRunner) -> None:
        """Worker pane create routes through build_adhoc_plan + run_plan."""
        with patch.dict("os.environ", {"DGOV_SKIP_GOVERNOR_CHECK": "1"}):
            registry = {"qwen-35b": {"prompt_command": "claude"}}
            mock_result = MagicMock(run_id=42, status="submitted")
            with patch("dgov.agents.load_registry", return_value=registry):
                with patch("dgov.plan.build_adhoc_plan") as mock_build:
                    mock_build.return_value = MagicMock(name="fix-parser")
                    with patch("dgov.plan.write_adhoc_plan", return_value="/tmp/plan.toml"):
                        with patch("dgov.plan.run_plan", return_value=mock_result):
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
        assert "dag_run_id" in result.output
        mock_build.assert_called_once()

    def test_ltgov_uses_direct_dispatch(self, runner: CliRunner) -> None:
        """LT-GOV role bypasses plan pipeline, uses run_dispatch_only."""
        with patch.dict("os.environ", {"DGOV_SKIP_GOVERNOR_CHECK": "1"}):
            registry = {"codex-mini": {"prompt_command": "codex"}}
            with patch("dgov.agents.load_registry", return_value=registry):
                with patch("dgov.context_packet.build_context_packet"):
                    with patch("dgov.executor.run_dispatch_only") as mock_dispatch:
                        mock_dispatch.return_value = MagicMock(
                            slug="audit-task",
                            pane_id="001",
                            agent="codex-mini",
                            worktree_path="/tmp/worktree",
                            branch_name="audit-task",
                        )
                        result = runner.invoke(
                            cli,
                            [
                                "pane",
                                "create",
                                "-a",
                                "codex-mini",
                                "-s",
                                "audit-task",
                                "-p",
                                "Audit the codebase",
                                "--no-preflight",
                                "--role",
                                "lt-gov",
                            ],
                        )

        assert result.exit_code == 0
        assert "pane_id" in result.output
        mock_dispatch.assert_called_once()


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


class TestPaneTail:
    def test_tail_pane_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.persistence.get_pane", return_value=None):
            result = runner.invoke(cli, ["pane", "tail", "ghost", "-r", "."])
        assert result.exit_code != 0
        assert "not found" in (result.output + (result.stderr or "")).lower()

    def test_tail_terminal_state_shows_status(self, runner: CliRunner) -> None:
        pane = {"slug": "done-task", "state": "merged", "role": "worker"}
        with (
            patch("dgov.persistence.get_pane", return_value=pane),
            patch("dgov.status.tail_worker_log", return_value="final output"),
        ):
            result = runner.invoke(cli, ["pane", "tail", "done-task", "-r", "."])
        assert result.exit_code == 0
        assert "final output" in result.output
        assert "merged" in result.output

    def test_tail_failed_state_shows_yellow(self, runner: CliRunner) -> None:
        pane = {"slug": "bad-task", "state": "failed", "role": "worker"}
        with (
            patch("dgov.persistence.get_pane", return_value=pane),
            patch("dgov.status.tail_worker_log", return_value=None),
            patch("dgov.status.capture_worker_output", return_value="error log"),
        ):
            result = runner.invoke(cli, ["pane", "tail", "bad-task", "-r", "."])
        assert result.exit_code == 0
        assert "failed" in result.output
