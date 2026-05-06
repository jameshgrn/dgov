"""Tests for `dgov ledger` CLI commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import list_ledger_entries

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_probation_ledger_add_accepts_affected_paths(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        result = runner.invoke(
            cli,
            [
                "ledger",
                "add",
                "rule",
                "Kernel path rule",
                "--path",
                "src/dgov",
                "--path",
                "tests/test_kernel.py",
            ],
        )

        assert result.exit_code == 0, result.output
        entries = list_ledger_entries(str(root))
        assert len(entries) == 1
        assert entries[0].affected_paths == ("src/dgov", "tests/test_kernel.py")


def test_ledger_add_and_list_decision_category(runner: CliRunner, tmp_path: Path) -> None:
    """Test that 'decision' category can be added and listed."""
    with runner.isolated_filesystem(temp_dir=tmp_path) as _td:
        # Add a decision entry
        add_result = runner.invoke(
            cli,
            ["ledger", "add", "decision", "Use shared tuple for categories"],
        )
        assert add_result.exit_code == 0, add_result.output
        assert "Added decision entry" in add_result.output

        # List all entries (should find the decision)
        list_result = runner.invoke(cli, ["ledger", "list"])
        assert list_result.exit_code == 0, list_result.output
        assert "[decision]" in list_result.output
        assert "Use shared tuple for categories" in list_result.output

        # List filtered by decision category
        list_filtered = runner.invoke(cli, ["ledger", "list", "-c", "decision"])
        assert list_filtered.exit_code == 0, list_filtered.output
        assert "[decision]" in list_filtered.output
        assert "Use shared tuple for categories" in list_filtered.output
