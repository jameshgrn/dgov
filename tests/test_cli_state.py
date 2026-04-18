"""Tests for removed manual state-repair commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_retry_command_removed(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["retry", "tasks/main.alpha"])

    assert result.exit_code != 0
    assert "No such command 'retry'" in result.output


def test_mark_done_command_removed(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["mark-done", "tasks/main.alpha"])

    assert result.exit_code != 0
    assert "No such command 'mark-done'" in result.output


def test_recover_command_removed(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["recover"])

    assert result.exit_code != 0
    assert "No such command 'recover'" in result.output
