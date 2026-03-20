"""Tests for dgov init and dgov doctor commands."""

from __future__ import annotations

from pathlib import Path
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


class TestInit:
    def test_init_creates_config(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["init", "-r", str(tmp_path)], input="claude\nacceptEdits\n")
        assert result.exit_code == 0
        assert "Initialized dgov project" in result.output

        config = tmp_path / ".dgov" / "config.toml"
        assert config.is_file()
        content = config.read_text()
        assert 'governor_agent = "claude"' in content
        assert 'governor_permissions = "bypassPermissions"' in content

        assert (tmp_path / ".dgov" / "hooks").is_dir()
        assert (tmp_path / ".dgov" / "templates").is_dir()
        assert (tmp_path / ".dgov" / "batch").is_dir()

        gitignore = tmp_path / ".gitignore"
        assert gitignore.is_file()
        assert ".dgov/" in gitignore.read_text()

    def test_init_already_initialized(self, runner: CliRunner, tmp_path: Path) -> None:
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "config.toml").write_text('[dgov]\ngovernor_agent = "claude"\n')

        result = runner.invoke(cli, ["init", "-r", str(tmp_path)])
        assert result.exit_code == 0
        assert "Already initialized" in result.output

    def test_init_appends_to_existing_gitignore(self, runner: CliRunner, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n")

        result = runner.invoke(cli, ["init", "-r", str(tmp_path)], input="claude\nacceptEdits\n")
        assert result.exit_code == 0
        content = gitignore.read_text()
        assert "*.pyc" in content
        assert ".dgov/" in content

    def test_init_skips_gitignore_if_already_present(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".dgov/\n")

        result = runner.invoke(cli, ["init", "-r", str(tmp_path)], input="claude\nacceptEdits\n")
        assert result.exit_code == 0
        assert gitignore.read_text().count(".dgov/") == 1


class TestDoctor:
    def test_doctor_basic(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
            patch(
                "dgov.agents.detect_installed_agents",
                return_value=["claude"],
            ),
            patch(
                "platform.python_version_tuple",
                return_value=("3", "13", "0"),
            ),
            patch("platform.python_version", return_value="3.13.0"),
        ):
            result = runner.invoke(cli, ["doctor", "-r", str(tmp_path)])

        assert result.exit_code == 0
        assert "[ok] tmux installed" in result.output
        assert "[ok] git installed" in result.output
        assert "[ok] Python >= 3.12" in result.output
        assert "All checks passed" in result.output

    def test_doctor_fails_missing_tmux(self, runner: CliRunner, tmp_path: Path) -> None:
        def which_side_effect(name):
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        with (
            patch("shutil.which", side_effect=which_side_effect),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
            patch(
                "dgov.agents.detect_installed_agents",
                return_value=["claude"],
            ),
            patch(
                "platform.python_version_tuple",
                return_value=("3", "13", "0"),
            ),
            patch("platform.python_version", return_value="3.13.0"),
        ):
            result = runner.invoke(cli, ["doctor", "-r", str(tmp_path)])

        assert result.exit_code == 1
        assert "[FAIL] tmux installed" in result.output
        assert "Some checks failed" in result.output
