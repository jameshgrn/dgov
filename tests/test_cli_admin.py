"""CLI tests for dgov admin commands."""

from __future__ import annotations

import json
import os
import subprocess
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
        with patch("dgov.inspection.compute_stats", return_value={"total": 5}) as mock_stats:
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

    def test_dashboard_inline_calls_run(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch(
            "dgov.dashboard.run_dashboard",
            return_value=None,
        ) as mock_run:
            result = runner.invoke(cli, ["dashboard", "-r", str(tmp_path)])

        assert result.exit_code == 0

        mock_run.assert_called_once()
        called_project_root, called_session_root, called_refresh = mock_run.call_args[0]
        assert called_project_root == os.path.abspath(str(tmp_path))
        assert called_session_root is None
        assert called_refresh == 1.0


class TestInitCmd:
    def test_init_writes_bypass_permissions_without_second_prompt(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(cli, ["init", "-r", str(tmp_path)], input="claude\n")

        assert result.exit_code == 0
        assert "Permission mode" not in result.output

        config_path = tmp_path / ".dgov" / "config.toml"
        assert config_path.is_file()
        config_content = config_path.read_text(encoding="utf-8")
        assert 'governor_agent = "claude"' in config_content
        assert 'governor_permissions = "bypassPermissions"' in config_content


class TestCodebaseCmd:
    def test_codebase_dry_run(self, runner: CliRunner) -> None:
        """Test codebase command dry-run mode."""
        result = runner.invoke(cli, ["codebase", "--dry-run", "-r", "."])

        assert result.exit_code == 0
        assert "# dgov Codebase Map" in result.output
        assert "## Task routing — start here" in result.output
        assert "## Invariants" in result.output
        assert "Module groups" in result.output

    def test_codebase_write_file(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test codebase command writes CODEBASE.md."""
        result = runner.invoke(cli, ["codebase", "-r", str(tmp_path)])

        assert result.exit_code == 0
        codebase_path = tmp_path / "CODEBASE.md"
        assert codebase_path.is_file()

        content = codebase_path.read_text(encoding="utf-8")
        assert "# dgov Codebase Map" in content
        assert "## Module groups" in content

    def test_codebase_commit_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test codebase command with --commit flag."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

        # Create CODEBASE.md file to simulate regeneration
        codebase_path = tmp_path / "CODEBASE.md"
        codebase_path.write_text("# dgov Codebase Map\n", encoding="utf-8")

        with patch("subprocess.run") as mock_subprocess:
            result = runner.invoke(cli, ["codebase", "--commit", "-r", str(tmp_path)])

        assert result.exit_code == 0
        assert "Written to" in result.output

        # Verify git add was called with env containing DGOV_SKIP_GOVERNOR_CHECK=1
        subprocess_add_calls = [
            call
            for call in mock_subprocess.call_args_list
            if len(call[0]) > 0 and call[0][0] == ["git", "add", "CODEBASE.md"]
        ]
        assert len(subprocess_add_calls) == 1
        env = subprocess_add_calls[0][1]["env"]
        assert env.get("DGOV_SKIP_GOVERNOR_CHECK") == "1"

        # Verify git commit was called with env containing DGOV_SKIP_GOVERNOR_CHECK=1
        subprocess_commit_calls = [
            call
            for call in mock_subprocess.call_args_list
            if len(call[0]) > 0 and call[0][0] == ["git", "commit", "-m", "Regenerate CODEBASE.md"]
        ]
        assert len(subprocess_commit_calls) == 1
        env = subprocess_commit_calls[0][1]["env"]
        assert env.get("DGOV_SKIP_GOVERNOR_CHECK") == "1"


class TestTranscriptCmd:
    def test_transcript_missing_file(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test transcript command with non-existent file."""
        result = runner.invoke(cli, ["transcript", "nonexistent", "-r", str(tmp_path)])

        assert result.exit_code == 1
        assert "No transcript found" in result.output

    def test_transcript_json_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test transcript command JSON output mode."""
        logs_dir = tmp_path / ".dgov" / "logs"
        logs_dir.mkdir(parents=True)
        transcript_path = logs_dir / "test-task.transcript.jsonl"

        entry = {
            "type": "message",
            "id": "test-id",
            "timestamp": "2026-03-19T18:15:26.203Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        }
        transcript_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        result = runner.invoke(cli, ["transcript", "test-task", "--json", "-r", str(tmp_path)])

        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert data["type"] == "message"
        assert data["message"]["content"][0]["text"] == "Hello"

    def test_transcript_summary_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test transcript command summary output mode."""
        logs_dir = tmp_path / ".dgov" / "logs"
        logs_dir.mkdir(parents=True)
        transcript_path = logs_dir / "test-task.transcript.jsonl"

        # Add user and assistant message entries
        lines = [
            json.dumps(
                {
                    "type": "message",
                    "id": "1",
                    "timestamp": "2026-03-19T18:15:26.203Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "id": "2",
                    "timestamp": "2026-03-19T18:15:27.203Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi there!"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "model_change",
                    "id": "3",
                    "timestamp": "2026-03-19T18:15:28.203Z",
                }
            ),
        ]
        transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = runner.invoke(cli, ["transcript", "test-task", "-r", str(tmp_path)])

        assert result.exit_code == 0
        assert "Hi there!" in result.output
        assert "[18:15:27]" in result.output
        # User message and model_change should be skipped
        assert "Hello" not in result.output
