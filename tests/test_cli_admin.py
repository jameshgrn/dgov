"""CLI tests for dgov admin commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
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


class TestVersionCmd:
    def test_version_outputs_json(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["version"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "dgov" in data


class TestStatusCmd:
    def test_status_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("dgov.status.list_worker_panes", return_value=[]):
            result = runner.invoke(cli, ["status", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["panes"] == []

    def test_status_with_panes(self, runner: CliRunner, tmp_path: Path) -> None:
        panes = [
            {
                "slug": "active-pane",
                "alive": True,
                "state": "active",
            },
            {
                "slug": "done-pane",
                "alive": False,
                "state": "done",
            },
        ]
        with patch("dgov.status.list_worker_panes", return_value=panes):
            result = runner.invoke(cli, ["status", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 2
        assert data["alive"] == 1
        assert data["done"] == 1
        assert data["merged"] == 0
        assert data["failed"] == 0


class TestRebaseCmd:
    def test_rebase_success(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch(
            "dgov.inspection.rebase_governor",
            return_value={"rebased": True},
        ):
            result = runner.invoke(cli, ["rebase", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["rebased"] is True

    def test_rebase_failure(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch(
            "dgov.inspection.rebase_governor",
            return_value={"rebased": False, "error": "conflict"},
        ):
            result = runner.invoke(cli, ["rebase", "-r", str(tmp_path)])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["rebased"] is False
        assert data["error"] == "conflict"


class TestBlameCmd:
    def test_blame_file_level(self, runner: CliRunner, tmp_path: Path) -> None:
        result_data = {"file": "test.py", "touches": []}
        with patch("dgov.blame.blame_file", return_value=result_data) as mock_blame:
            result = runner.invoke(cli, ["blame", "test.py", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == result_data

        mock_blame.assert_called_once_with(
            project_root=os.path.abspath(str(tmp_path)),
            file_path="test.py",
            session_root=None,
            last_only=True,
            agent_filter=None,
        )

    def test_blame_line_level(self, runner: CliRunner, tmp_path: Path) -> None:
        result_data = {"lines": []}
        with patch("dgov.blame.blame_lines", return_value=result_data) as mock_blame:
            result = runner.invoke(
                cli,
                ["blame", "test.py", "--line-level", "-r", str(tmp_path)],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == result_data

        mock_blame.assert_called_once_with(
            project_root=os.path.abspath(str(tmp_path)),
            file_path="test.py",
            session_root=None,
            start_line=None,
            end_line=None,
            agent_filter=None,
        )

    def test_blame_line_range(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("dgov.blame.blame_lines", return_value={"lines": []}) as mock_blame:
            result = runner.invoke(
                cli,
                ["blame", "test.py", "--line-level", "-L", "10-20", "-r", str(tmp_path)],
            )

        assert result.exit_code == 0

        mock_blame.assert_called_once_with(
            project_root=os.path.abspath(str(tmp_path)),
            file_path="test.py",
            session_root=None,
            start_line=10,
            end_line=20,
            agent_filter=None,
        )


class TestListAgentsCmd:
    def test_agents_lists_installed(self, runner: CliRunner, tmp_path: Path) -> None:
        agent_def = SimpleNamespace(
            name="Claude",
            prompt_transport="positional",
            source="builtin",
            health_check=None,
        )
        registry = {"claude": agent_def}

        with (
            patch("dgov.agents.load_registry", return_value=registry),
            patch(
                "dgov.cli.admin.detect_installed_agents",
                return_value=["claude"],
            ),
        ):
            result = runner.invoke(cli, ["agents", "-r", str(tmp_path)])

        assert result.exit_code == 0
        agents = json.loads(result.output)
        assert len(agents) == 1
        agent = agents[0]
        assert agent["id"] == "claude"
        assert agent["installed"] is True


class TestStatsCmd:
    def test_stats_outputs_json(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("dgov.metrics.compute_stats", return_value={"total": 5}) as mock_stats:
            result = runner.invoke(cli, ["stats", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"total": 5}

        session_root = os.path.abspath(str(tmp_path))
        mock_stats.assert_called_once_with(session_root)


class TestDashboardCmd:
    def test_dashboard_pane_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            result = runner.invoke(cli, ["dashboard", "--pane", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"dashboard": "launched in pane"}

        project_root = os.path.abspath(str(tmp_path))
        expected_cmd = f"dgov dashboard -r {project_root} --refresh 1.0"
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0][0:4] == ["tmux", "split-window", "-d", "-l"]
        assert expected_cmd in args[0][-1]
        assert kwargs.get("check") is True

    def test_dashboard_inline_calls_v2(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch(
            "dgov.dashboard_v2.run_dashboard_v2",
            return_value=None,
        ) as mock_run:
            result = runner.invoke(cli, ["dashboard", "-r", str(tmp_path)])

        assert result.exit_code == 0

        mock_run.assert_called_once()
        called_project_root, called_session_root, called_refresh = mock_run.call_args[0]
        assert called_project_root == os.path.abspath(str(tmp_path))
        assert called_session_root is None
        assert called_refresh == 1.0
