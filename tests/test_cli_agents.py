"""Tests for dgov agent guidance commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


def test_agents_sync_command_writes_shipped_skills(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    result = CliRunner().invoke(cli, ["agents", "sync", "--skills-dir", str(skills_dir)])

    assert result.exit_code == 0
    assert "Synced dgov agent skills" in result.output
    assert (skills_dir / "dgov-ledger" / "SKILL.md").exists()
    assert (skills_dir / "dgov-plan" / "SKILL.md").exists()
    assert (skills_dir / "dgov-pane" / "SKILL.md").exists()
