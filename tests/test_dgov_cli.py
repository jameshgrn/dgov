"""Tests for dgov.cli — Click CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# CLI group structure
# ---------------------------------------------------------------------------


class TestCliGroup:
    def test_cli_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "dgov" in result.output

    def test_pane_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "--help"])
        assert result.exit_code == 0
        assert "Manage worker panes" in result.output

    def test_pane_subcommands_listed(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "--help"])
        for cmd in ["create", "close", "merge", "wait", "list", "prune", "classify", "capture"]:
            assert cmd in result.output


# ---------------------------------------------------------------------------
# pane list
# ---------------------------------------------------------------------------


class TestPaneList:
    def test_empty_list(self, runner: CliRunner) -> None:
        with (
            patch("dgov.cli.click.echo") as _,
            patch("dgov.panes.list_worker_panes", return_value=[]),
        ):
            result = runner.invoke(cli, ["pane", "list", "-r", "/tmp"])
            assert result.exit_code == 0

    def test_returns_json(self, runner: CliRunner) -> None:
        mock_panes = [{"slug": "test-1", "agent": "pi", "done": False}]
        with patch("dgov.panes.list_worker_panes", return_value=mock_panes):
            result = runner.invoke(cli, ["pane", "list", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["slug"] == "test-1"


# ---------------------------------------------------------------------------
# pane classify
# ---------------------------------------------------------------------------


class TestPaneClassify:
    def test_classify_returns_json(self, runner: CliRunner) -> None:
        with patch("dgov.panes.classify_task", return_value="pi"):
            result = runner.invoke(cli, ["pane", "classify", "fix the lint errors"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["recommended_agent"] == "pi"
            assert "fix the lint" in data["prompt_preview"]

    def test_classify_claude(self, runner: CliRunner) -> None:
        with patch("dgov.panes.classify_task", return_value="claude"):
            result = runner.invoke(cli, ["pane", "classify", "refactor the whole module"])
            data = json.loads(result.output)
            assert data["recommended_agent"] == "claude"


# ---------------------------------------------------------------------------
# pane prune
# ---------------------------------------------------------------------------


class TestPanePrune:
    def test_prune_returns_json(self, runner: CliRunner) -> None:
        with patch("dgov.panes.prune_stale_panes", return_value=["dead-slug"]):
            result = runner.invoke(cli, ["pane", "prune", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["pruned"] == ["dead-slug"]

    def test_prune_empty(self, runner: CliRunner) -> None:
        with patch("dgov.panes.prune_stale_panes", return_value=[]):
            result = runner.invoke(cli, ["pane", "prune", "-r", "/tmp"])
            data = json.loads(result.output)
            assert data["pruned"] == []


# ---------------------------------------------------------------------------
# pane close
# ---------------------------------------------------------------------------


class TestPaneClose:
    def test_close_success(self, runner: CliRunner) -> None:
        with patch("dgov.panes.close_worker_pane", return_value=True):
            result = runner.invoke(cli, ["pane", "close", "my-slug", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["closed"] == "my-slug"

    def test_close_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.panes.close_worker_pane", return_value=False):
            result = runner.invoke(cli, ["pane", "close", "missing", "-r", "/tmp"])
            assert result.exit_code == 1


# ---------------------------------------------------------------------------
# pane merge
# ---------------------------------------------------------------------------


class TestPaneMerge:
    def test_merge_success_default_close(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"merged": "slug", "branch": "slug"},
        ):
            result = runner.invoke(cli, ["pane", "merge", "slug", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["merged"] == "slug"

    def test_merge_no_close(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane",
            return_value={"merged": "slug", "files_changed": 3},
        ):
            result = runner.invoke(cli, ["pane", "merge", "slug", "-r", "/tmp", "--no-close"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["merged"] == "slug"

    def test_merge_error_exit_1(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"error": "conflicts detected"},
        ):
            result = runner.invoke(cli, ["pane", "merge", "slug", "-r", "/tmp"])
            assert result.exit_code == 1

    def test_merge_registered_exits_ok(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"registered": True},
        ):
            result = runner.invoke(cli, ["pane", "merge", "slug", "-r", "/tmp"])
            assert result.exit_code == 0

    def test_merge_resolve_option(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane",
            return_value={"merged": "slug"},
        ) as mock_merge:
            result = runner.invoke(
                cli, ["pane", "merge", "slug", "-r", "/tmp", "--no-close", "--resolve", "manual"]
            )
            assert result.exit_code == 0
            mock_merge.assert_called_once()
            assert mock_merge.call_args.kwargs.get("resolve") == "manual" or (
                len(mock_merge.call_args.args) > 2
            )


# ---------------------------------------------------------------------------
# pane capture
# ---------------------------------------------------------------------------


class TestPaneCapture:
    def test_capture_success(self, runner: CliRunner) -> None:
        with patch("dgov.panes.capture_worker_output", return_value="line 1\nline 2\n"):
            result = runner.invoke(cli, ["pane", "capture", "slug", "-r", "/tmp"])
            assert result.exit_code == 0
            assert "line 1" in result.output

    def test_capture_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.panes.capture_worker_output", return_value=None):
            result = runner.invoke(cli, ["pane", "capture", "slug", "-r", "/tmp"])
            assert result.exit_code == 1

    def test_capture_lines_option(self, runner: CliRunner) -> None:
        with patch("dgov.panes.capture_worker_output", return_value="output") as mock_cap:
            runner.invoke(cli, ["pane", "capture", "slug", "-r", "/tmp", "-n", "50"])
            assert mock_cap.call_args[1].get("lines") == 50 or mock_cap.call_args[0][2] == 50


# ---------------------------------------------------------------------------
# pane review
# ---------------------------------------------------------------------------


class TestPaneReview:
    def test_review_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.review_worker_pane",
            return_value={"stat": "+10 -5", "files": ["a.py"]},
        ):
            result = runner.invoke(cli, ["pane", "review", "slug", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "stat" in data

    def test_review_error(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.review_worker_pane",
            return_value={"error": "no branch found"},
        ):
            result = runner.invoke(cli, ["pane", "review", "slug", "-r", "/tmp"])
            assert result.exit_code == 1


# ---------------------------------------------------------------------------
# pane escalate
# ---------------------------------------------------------------------------


class TestPaneEscalate:
    def test_escalate_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.escalate_worker_pane",
            return_value={"escalated": "slug", "new_agent": "claude"},
        ):
            result = runner.invoke(cli, ["pane", "escalate", "slug", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["new_agent"] == "claude"

    def test_escalate_error(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.escalate_worker_pane",
            return_value={"error": "pane not found"},
        ):
            result = runner.invoke(cli, ["pane", "escalate", "slug", "-r", "/tmp"])
            assert result.exit_code == 1


# ---------------------------------------------------------------------------
# pane create
# ---------------------------------------------------------------------------


class TestPaneCreate:
    def test_create_success(self, runner: CliRunner) -> None:
        mock_pane = MagicMock()
        mock_pane.slug = "test-slug"
        mock_pane.pane_id = "%5"
        mock_pane.agent = "pi"
        mock_pane.worktree_path = "/tmp/wt"
        mock_pane.branch_name = "wt/test-slug"
        with patch("dgov.panes.create_worker_pane", return_value=mock_pane):
            result = runner.invoke(
                cli,
                ["pane", "create", "-a", "pi", "-p", "fix lint", "-r", "/tmp", "--no-preflight"],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["slug"] == "test-slug"
            assert data["agent"] == "pi"

    def test_create_unknown_agent(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli, ["pane", "create", "-a", "unknown_agent", "-p", "fix stuff", "-r", "/tmp"]
        )
        assert result.exit_code == 1

    def test_create_auto_classifies(self, runner: CliRunner) -> None:
        mock_pane = MagicMock()
        mock_pane.slug = "s"
        mock_pane.pane_id = "%1"
        mock_pane.agent = "pi"
        mock_pane.worktree_path = "/tmp/wt"
        mock_pane.branch_name = "b"
        with (
            patch("dgov.panes.classify_task", return_value="pi") as mock_classify,
            patch("dgov.panes.create_worker_pane", return_value=mock_pane),
        ):
            result = runner.invoke(
                cli,
                ["pane", "create", "-a", "auto", "-p", "fix lint", "-r", "/tmp", "--no-preflight"],
            )
            assert result.exit_code == 0
            mock_classify.assert_called_once_with("fix lint")

    def test_create_env_parsing(self, runner: CliRunner) -> None:
        mock_pane = MagicMock()
        mock_pane.slug = "s"
        mock_pane.pane_id = "%1"
        mock_pane.agent = "pi"
        mock_pane.worktree_path = "/tmp/wt"
        mock_pane.branch_name = "b"
        with patch("dgov.panes.create_worker_pane", return_value=mock_pane) as mock_create:
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "-a",
                    "pi",
                    "-p",
                    "task",
                    "-r",
                    "/tmp",
                    "-e",
                    "FOO=bar",
                    "-e",
                    "BAZ=qux",
                    "--no-preflight",
                ],
            )
            assert result.exit_code == 0
            call_kwargs = mock_create.call_args[1]
            assert call_kwargs["env_vars"] == {"FOO": "bar", "BAZ": "qux"}

    def test_create_invalid_env(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            [
                "pane",
                "create",
                "-a",
                "pi",
                "-p",
                "task",
                "-r",
                "/tmp",
                "-e",
                "NOEQUALS",
                "--no-preflight",
            ],
        )
        assert result.exit_code == 1

    def test_create_requires_prompt(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "create", "-a", "pi", "-r", "/tmp"])
        assert result.exit_code != 0  # missing required --prompt


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_json(self, runner: CliRunner) -> None:
        mock_status = {"panes": [], "tunnel": "up"}
        with patch("dgov.state.get_status", return_value=mock_status):
            result = runner.invoke(cli, ["status", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["tunnel"] == "up"


# ---------------------------------------------------------------------------
# rebase
# ---------------------------------------------------------------------------


class TestRebase:
    def test_rebase_success(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": True, "onto": "main"},
        ):
            result = runner.invoke(cli, ["rebase", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["rebased"] is True

    def test_rebase_failure(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": False, "error": "conflicts"},
        ):
            result = runner.invoke(cli, ["rebase", "-r", "/tmp"])
            assert result.exit_code == 1

    def test_rebase_onto_option(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": True},
        ) as mock_rebase:
            result = runner.invoke(cli, ["rebase", "-r", "/tmp", "--onto", "develop"])
            assert result.exit_code == 0
            mock_rebase.assert_called_once_with("/tmp", onto="develop")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class TestAgents:
    def test_agents_lists_all(self, runner: CliRunner) -> None:
        with patch("dgov.cli.detect_installed_agents", return_value=["claude", "pi"]):
            result = runner.invoke(cli, ["agents"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            ids = {a["id"] for a in data}
            assert "claude" in ids
            assert "pi" in ids
            # claude should be marked installed
            claude = next(a for a in data if a["id"] == "claude")
            assert claude["installed"] is True


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_preflight_pass(self, runner: CliRunner) -> None:
        mock_report = MagicMock()
        mock_report.passed = True
        mock_report.to_dict.return_value = {"passed": True, "checks": []}
        with patch("dgov.preflight.run_preflight", return_value=mock_report):
            result = runner.invoke(cli, ["preflight", "-r", "/tmp"])
            assert result.exit_code == 0

    def test_preflight_fail(self, runner: CliRunner) -> None:
        mock_report = MagicMock()
        mock_report.passed = False
        mock_report.to_dict.return_value = {"passed": False, "checks": [{"fail": "dirty tree"}]}
        with patch("dgov.preflight.run_preflight", return_value=mock_report):
            result = runner.invoke(cli, ["preflight", "-r", "/tmp"])
            assert result.exit_code == 1

    def test_preflight_fix(self, runner: CliRunner) -> None:
        mock_report_fail = MagicMock()
        mock_report_fail.passed = False
        mock_report_fixed = MagicMock()
        mock_report_fixed.passed = True
        mock_report_fixed.to_dict.return_value = {"passed": True}
        with (
            patch("dgov.preflight.run_preflight", return_value=mock_report_fail),
            patch("dgov.preflight.fix_preflight", return_value=mock_report_fixed),
        ):
            result = runner.invoke(cli, ["preflight", "-r", "/tmp", "--fix"])
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# merge-all
# ---------------------------------------------------------------------------


class TestMergeAll:
    def test_merge_all_no_done(self, runner: CliRunner) -> None:
        with patch("dgov.panes.list_worker_panes", return_value=[]):
            result = runner.invoke(cli, ["pane", "merge-all", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["skipped"] == "no done panes"

    def test_merge_all_success(self, runner: CliRunner) -> None:
        mock_panes = [
            {"slug": "a", "done": True},
            {"slug": "b", "done": True},
            {"slug": "c", "done": False},
        ]
        with (
            patch("dgov.panes.list_worker_panes", return_value=mock_panes),
            patch(
                "dgov.panes.merge_worker_pane_with_close",
                return_value={"merged": "ok", "files_changed": 2},
            ) as mock_merge,
        ):
            result = runner.invoke(cli, ["pane", "merge-all", "-r", "/tmp"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["merged_count"] == 2
            assert set(data["merged"]) == {"a", "b"}
            assert set(data["closed"]) == {"a", "b"}
            assert mock_merge.call_count == 2

    def test_merge_all_no_close(self, runner: CliRunner) -> None:
        mock_panes = [
            {"slug": "a", "done": True},
            {"slug": "b", "done": True},
        ]
        with (
            patch("dgov.panes.list_worker_panes", return_value=mock_panes),
            patch(
                "dgov.panes.merge_worker_pane",
                return_value={"merged": "ok", "files_changed": 1},
            ) as mock_merge,
        ):
            result = runner.invoke(cli, ["pane", "merge-all", "-r", "/tmp", "--no-close"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["merged_count"] == 2
            assert "closed" not in data
            assert mock_merge.call_count == 2

    def test_merge_all_with_failure(self, runner: CliRunner) -> None:
        mock_panes = [{"slug": "a", "done": True}]
        with (
            patch("dgov.panes.list_worker_panes", return_value=mock_panes),
            patch(
                "dgov.panes.merge_worker_pane_with_close",
                return_value={"error": "conflict"},
            ),
        ):
            result = runner.invoke(cli, ["pane", "merge-all", "-r", "/tmp"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["failed_count"] == 1


class TestAgentsCommand:
    def test_agents_list(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.agents.detect_installed_agents",
            return_value=["pi", "claude"],
        ):
            result = runner.invoke(cli, ["agents"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert isinstance(output, list)
        assert len(output) > 0
        assert all("id" in a for a in output)


# ---------------------------------------------------------------------------
# merge-all
# ---------------------------------------------------------------------------


class TestHelpOutput:
    def test_main_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "dgov" in result.output

    def test_pane_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "--help"])
        assert result.exit_code == 0
        assert "Manage worker panes" in result.output

    def test_pane_create_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "create", "--help"])
        assert result.exit_code == 0
        assert "--agent" in result.output
        assert "--prompt" in result.output
        assert "--slug" in result.output

    def test_pane_close_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "close", "--help"])
        assert result.exit_code == 0
        assert "SLUG" in result.output

    def test_pane_merge_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "merge", "--help"])
        assert result.exit_code == 0
        assert "--resolve" in result.output
        assert "--close" in result.output

    def test_pane_wait_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "wait", "--help"])
        assert result.exit_code == 0
        assert "--timeout" in result.output
        assert "--poll" in result.output

    def test_pane_wait_all_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "wait-all", "--help"])
        assert result.exit_code == 0
        assert "--timeout" in result.output

    def test_pane_merge_all_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "merge-all", "--help"])
        assert result.exit_code == 0
        assert "--resolve" in result.output

    def test_pane_classify_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "classify", "--help"])
        assert result.exit_code == 0
        assert "PROMPT" in result.output

    def test_pane_review_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "review", "--help"])
        assert result.exit_code == 0
        assert "--full" in result.output

    def test_pane_escalate_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "escalate", "--help"])
        assert result.exit_code == 0
        assert "--agent" in result.output

    def test_status_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output.lower()

    def test_rebase_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["rebase", "--help"])
        assert result.exit_code == 0
        assert "--onto" in result.output

    def test_preflight_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["preflight", "--help"])
        assert result.exit_code == 0
        assert "--fix" in result.output

    def test_agents_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["agents", "--help"])
        assert result.exit_code == 0
        assert "agent" in result.output.lower()


# ---------------------------------------------------------------------------
# pane create
# ---------------------------------------------------------------------------


class TestMergeAllCommand:
    def test_no_done_panes(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch("dgov.panes.list_worker_panes", return_value=[]):
            result = runner.invoke(cli, ["pane", "merge-all"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["merged"] == []

    def test_merge_all_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        panes = [
            {"slug": "t1", "done": True},
            {"slug": "t2", "done": True},
            {"slug": "t3", "done": False},
        ]
        merge_results = [
            {"merged": "t1", "branch": "t1", "files_changed": 2},
            {"merged": "t2", "branch": "t2", "files_changed": 1},
        ]

        with (
            patch("dgov.panes.list_worker_panes", return_value=panes),
            patch("dgov.panes.merge_worker_pane_with_close", side_effect=merge_results),
        ):
            result = runner.invoke(cli, ["pane", "merge-all"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["merged_count"] == 2
        assert output["failed_count"] == 0
        assert set(output["closed"]) == {"t1", "t2"}

    def test_merge_all_with_failure(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        panes = [{"slug": "t1", "done": True}]
        with (
            patch("dgov.panes.list_worker_panes", return_value=panes),
            patch(
                "dgov.panes.merge_worker_pane_with_close",
                return_value={"error": "conflict"},
            ),
        ):
            result = runner.invoke(cli, ["pane", "merge-all"])
        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["failed_count"] == 1


class TestPaneCaptureCommand:
    """Tests for pane capture subcommand."""

    def test_pane_capture_success(self, runner: CliRunner) -> None:
        """Monkeypatch capture to return text, verify output."""
        from unittest.mock import patch

        mock_output = "line1\nline2\nline3"

        with patch("dgov.panes.capture_worker_output", return_value=mock_output):
            result = runner.invoke(cli, ["pane", "capture", "my-task"])

        assert result.exit_code == 0
        assert mock_output in result.output

    def test_pane_capture_missing(self, runner: CliRunner) -> None:
        """Monkeypatch capture to raise, verify error exit."""
        from unittest.mock import patch

        with patch("dgov.panes.capture_worker_output", return_value=None):
            result = runner.invoke(cli, ["pane", "capture", "missing-task"])

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert "error" in output
        assert "missing-task" in output["error"]


class TestPaneClassifyCommand:
    def test_classify(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch("dgov.panes.classify_task", return_value="claude"):
            result = runner.invoke(cli, ["pane", "classify", "debug flaky test"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["recommended_agent"] == "claude"


# ---------------------------------------------------------------------------
# pane review
# ---------------------------------------------------------------------------


class TestPaneCloseCommand:
    def test_close_not_found(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch("dgov.panes.close_worker_pane", return_value=False):
            result = runner.invoke(cli, ["pane", "close", "missing"])
        assert result.exit_code == 1
        output = json.loads(result.output)
        assert "error" in output

    def test_close_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch("dgov.panes.close_worker_pane", return_value=True):
            result = runner.invoke(cli, ["pane", "close", "my-task"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["closed"] == "my-task"


# ---------------------------------------------------------------------------
# pane merge
# ---------------------------------------------------------------------------


class TestPaneCreateCommand:
    def test_unknown_agent_exits_1(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli, ["pane", "create", "--agent", "nonexistent-agent", "--prompt", "test"]
        )
        assert result.exit_code == 1
        assert "Unknown agent" in result.output

    def test_invalid_env_var(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch("dgov.panes.create_worker_pane"):
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "--agent",
                    "pi",
                    "--prompt",
                    "x",
                    "-e",
                    "BADFORMAT",
                    "--no-preflight",
                ],
            )
        assert result.exit_code == 1
        assert "KEY=VALUE" in result.output

    def test_auto_classify(self, runner: CliRunner) -> None:
        from unittest.mock import MagicMock, patch

        mock_pane = MagicMock()
        mock_pane.slug = "test-slug"
        mock_pane.pane_id = "%5"
        mock_pane.agent = "pi"
        mock_pane.worktree_path = "/tmp/wt"
        mock_pane.branch_name = "test-slug"

        with (
            patch("dgov.panes.classify_task", return_value="pi") as mock_classify,
            patch("dgov.panes.create_worker_pane", return_value=mock_pane),
        ):
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "--agent",
                    "auto",
                    "--prompt",
                    "fix typo",
                    "--no-preflight",
                ],
            )
        assert result.exit_code == 0
        mock_classify.assert_called_once_with("fix typo")

    def test_create_success(self, runner: CliRunner) -> None:
        from unittest.mock import MagicMock, patch

        mock_pane = MagicMock()
        mock_pane.slug = "my-task"
        mock_pane.pane_id = "%10"
        mock_pane.agent = "pi"
        mock_pane.worktree_path = "/tmp/wt/my-task"
        mock_pane.branch_name = "my-task"

        with patch("dgov.panes.create_worker_pane", return_value=mock_pane):
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "--agent",
                    "pi",
                    "--prompt",
                    "do stuff",
                    "--slug",
                    "my-task",
                    "--no-preflight",
                ],
            )
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["slug"] == "my-task"
        assert output["agent"] == "pi"


# ---------------------------------------------------------------------------
# pane close
# ---------------------------------------------------------------------------


class TestPaneEscalateCommand:
    def test_escalate_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        esc_result = {
            "escalated": True,
            "original_slug": "task-1",
            "new_slug": "task-1-esc",
            "agent": "claude",
        }
        with patch("dgov.panes.escalate_worker_pane", return_value=esc_result):
            result = runner.invoke(cli, ["pane", "escalate", "task-1"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["escalated"] is True

    def test_escalate_error(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.panes.escalate_worker_pane",
            return_value={"error": "not found"},
        ):
            result = runner.invoke(cli, ["pane", "escalate", "missing"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestPaneListCommand:
    """Tests for pane list subcommand."""

    def test_pane_list_empty(self, runner: CliRunner) -> None:
        """Monkeypatch list_worker_panes to return [], verify empty JSON array."""
        from unittest.mock import patch

        with patch("dgov.panes.list_worker_panes", return_value=[]):
            result = runner.invoke(cli, ["pane", "list"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_pane_list_with_panes(self, runner: CliRunner) -> None:
        """Monkeypatch to return pane dicts, verify JSON output."""
        from unittest.mock import patch

        mock_panes = [
            {
                "slug": "test-task",
                "agent": "pi",
                "done": True,
                "branch": "distributary/test-task-abc123",
                "worktree": "/tmp/worktrees/test-task",
            },
            {
                "slug": "another-task",
                "agent": "claude",
                "done": False,
                "branch": "distributary/another-task-def456",
                "worktree": "/tmp/worktrees/another-task",
            },
        ]

        with patch("dgov.panes.list_worker_panes", return_value=mock_panes):
            result = runner.invoke(cli, ["pane", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert len(output) == 2
        assert output[0]["slug"] == "test-task"
        assert output[1]["agent"] == "claude"


class TestPaneMergeCommand:
    def test_merge_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        merge_result = {"merged": "task-1", "branch": "task-1", "files_changed": 3}
        with patch("dgov.panes.merge_worker_pane", return_value=merge_result):
            result = runner.invoke(cli, ["pane", "merge", "task-1"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["merged"] == "task-1"

    def test_merge_error(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"error": "Merge failed"},
        ):
            result = runner.invoke(cli, ["pane", "merge", "task-1"])
        assert result.exit_code == 1

    def test_merge_conflicts(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        conflict_result = {
            "slug": "task-1",
            "branch": "task-1",
            "conflicts": ["src/foo.py"],
            "error": "conflicts detected",
        }
        with patch("dgov.panes.merge_worker_pane_with_close", return_value=conflict_result):
            result = runner.invoke(cli, ["pane", "merge", "task-1"])
        assert result.exit_code == 1

    def test_merge_default_closes(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        merge_result = {"merged": "task-1", "branch": "b"}
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value=merge_result,
        ):
            result = runner.invoke(cli, ["pane", "merge", "task-1"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["merged"] == "task-1"


# ---------------------------------------------------------------------------
# pane classify
# ---------------------------------------------------------------------------


class TestPanePruneCommand:
    """Tests for pane prune subcommand."""

    def test_pane_prune(self, runner: CliRunner) -> None:
        """Monkeypatch prune_stale_panes, verify it's called."""
        from unittest.mock import patch

        pruned_slugs = ["stale-task-1", "stale-task-2"]

        with patch("dgov.panes.prune_stale_panes", return_value=pruned_slugs) as mock_prune:
            result = runner.invoke(cli, ["pane", "prune"])

        assert result.exit_code == 0
        mock_prune.assert_called_once()
        output = json.loads(result.output)
        assert output["pruned"] == pruned_slugs


# ---------------------------------------------------------------------------
# Help smoke tests
# ---------------------------------------------------------------------------


class TestPaneReviewCommand:
    def test_review_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        review_result = {
            "slug": "task-1",
            "verdict": "safe",
            "commit_count": 2,
        }
        with patch("dgov.panes.review_worker_pane", return_value=review_result):
            result = runner.invoke(cli, ["pane", "review", "task-1"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["verdict"] == "safe"

    def test_review_error(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.panes.review_worker_pane",
            return_value={"error": "not found"},
        ):
            result = runner.invoke(cli, ["pane", "review", "missing"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# pane escalate
# ---------------------------------------------------------------------------


class TestRebaseCommand:
    def test_rebase_success(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": True, "base": "main"},
        ):
            result = runner.invoke(cli, ["rebase"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["rebased"] is True

    def test_rebase_failure(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": False, "error": "conflicts"},
        ):
            result = runner.invoke(cli, ["rebase"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def test_status_output(self, runner: CliRunner) -> None:
        from unittest.mock import patch

        mock_status = {"panes": [], "session_root": "/tmp"}
        with patch("dgov.state.get_status", return_value=mock_status):
            result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "panes" in output


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


class TestBatchCommand:
    def test_batch_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        spec = {
            "project_root": "/tmp/repo",
            "tasks": [
                {"id": "t1", "prompt": "do x", "touches": ["a.py"]},
                {"id": "t2", "prompt": "do y", "touches": ["b.py"]},
            ],
        }
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))
        result = runner.invoke(cli, ["batch", str(spec_file), "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["total_tasks"] == 2

    def test_batch_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["batch", "--help"])
        assert result.exit_code == 0
        assert "DAG-ordered" in result.output


# ---------------------------------------------------------------------------
# rebase
# ---------------------------------------------------------------------------
