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
