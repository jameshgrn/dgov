"""Unit tests for the `dgov fix` CLI command."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    """Prevent DGOV_JSON from leaking between tests."""
    import os

    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_compile(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the _cmd_compile function imported by fix.py."""
    mock = MagicMock()
    monkeypatch.setattr("dgov.cli.fix._cmd_compile", mock)
    return mock


@pytest.fixture
def mock_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the _cmd_run_plan function imported by fix.py."""
    mock = MagicMock()
    monkeypatch.setattr("dgov.cli.fix._cmd_run_plan", mock)
    return mock


class TestFixHappyPath:
    """Happy path tests for the fix command."""

    def test_creates_plan_tree_and_invokes_helpers(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """The command writes a single-task plan tree and invokes compile/run helpers."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["fix", "Refactor error handling", "--file", "src/utils.py"])

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"
        assert "Created plan" in result.output

        # Verify plan directory structure
        plan_dir = tmp_path / ".dgov" / "plans" / "fix-refactor-error-handling"
        assert plan_dir.exists()
        assert (plan_dir / "_root.toml").exists()
        assert (plan_dir / "fix" / "main.toml").exists()

        # Verify compile was called
        mock_compile.assert_called_once()
        call_args = mock_compile.call_args
        assert call_args.kwargs.get("dry_run") is False
        assert call_args.kwargs.get("recompile_sops") is False
        assert call_args.kwargs.get("graph") is False

        # Verify run was called
        mock_run.assert_called_once()

    def test_multiple_file_options_preserved(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Multiple --file options are preserved exactly in the generated task TOML."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            [
                "fix",
                "Update imports",
                "--file",
                "src/main.py",
                "--file",
                "src/config.py",
                "--file",
                "tests/test_main.py",
            ],
        )

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"

        plan_dir = tmp_path / ".dgov" / "plans" / "fix-update-imports"
        main_toml_path = plan_dir / "fix" / "main.toml"

        # Parse the generated TOML and verify the task claims
        content = main_toml_path.read_text()
        parsed = tomllib.loads(content)

        assert parsed["tasks"]["apply"]["files"] == [
            "src/main.py",
            "src/config.py",
            "tests/test_main.py",
        ]

    def test_name_override(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """--name overrides the generated plan name."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            [
                "fix",
                "Some prompt here",
                "--file",
                "src/foo.py",
                "--name",
                "my-custom-fix",
            ],
        )

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"
        assert "Created plan 'my-custom-fix'" in result.output

        # Verify plan directory uses custom name
        plan_dir = tmp_path / ".dgov" / "plans" / "my-custom-fix"
        assert plan_dir.exists()

        # Verify the name is in _root.toml
        root_toml = tomllib.loads((plan_dir / "_root.toml").read_text())
        assert root_toml["plan"]["name"] == "my-custom-fix"

    def test_commit_message_override(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """--commit-message overrides the default commit message."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            [
                "fix",
                "Fix the bug",
                "--file",
                "src/bug.py",
                "--commit-message",
                "fix: resolve critical bug in parser",
            ],
        )

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"

        plan_dir = tmp_path / ".dgov" / "plans" / "fix-fix-the-bug"
        main_toml_path = plan_dir / "fix" / "main.toml"

        parsed = tomllib.loads(main_toml_path.read_text())
        assert parsed["tasks"]["apply"]["commit_message"] == "fix: resolve critical bug in parser"


class TestFixEdgeCases:
    """Edge cases and error handling for the fix command."""

    def test_existing_plan_name_fails(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Explicit --name that already exists fails with clear error and non-zero exit code."""
        monkeypatch.chdir(tmp_path)

        # Create an existing plan
        plan_dir = tmp_path / ".dgov" / "plans" / "existing-plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "_root.toml").write_text('[plan]\nname = "existing-plan"\n')

        result = runner.invoke(
            cli,
            [
                "fix",
                "Some prompt",
                "--file",
                "src/foo.py",
                "--name",
                "existing-plan",
            ],
        )

        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        assert "already exists" in result.output.lower()
        assert "Use --name to specify a different name" in result.output

        # Compile and run should NOT have been called
        mock_compile.assert_not_called()
        mock_run.assert_not_called()

    def test_auto_generated_name_collision_uses_suffix(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_compile: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Auto-generated names should add a numeric suffix on collision."""
        monkeypatch.chdir(tmp_path)

        plan_dir = tmp_path / ".dgov" / "plans" / "fix-refactor-code"
        plan_dir.mkdir(parents=True)
        (plan_dir / "_root.toml").write_text('[plan]\nname = "fix-refactor-code"\n')

        result = runner.invoke(cli, ["fix", "Refactor code", "--file", "src/foo.py"])

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"
        assert "Created plan 'fix-refactor-code-2'" in result.output
        assert (tmp_path / ".dgov" / "plans" / "fix-refactor-code-2").exists()

    def test_missing_required_file_option(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing --file option should fail with clear error."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["fix", "Some prompt without files"])

        assert result.exit_code != 0
        assert "--file" in result.output or "required" in result.output.lower()


class TestFixHelp:
    """Help documentation tests for the fix command."""

    def test_help_renders_successfully(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dgov fix --help renders successfully and documents required options."""
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["fix", "--help"])

        assert result.exit_code == 0, f"Exit code: {result.exit_code}, output: {result.output}"

        output = result.output
        # Should mention the prompt argument
        assert "PROMPT" in output or "prompt" in output.lower()

        # Should document the --file option
        assert "--file" in output or "-f" in output

        # Should document the --name option
        assert "--name" in output

        # Should document the --commit-message option
        assert "--commit-message" in output
