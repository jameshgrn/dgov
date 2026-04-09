"""Tests for strict dgov run requirements (Plan Tree enforcement)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


def test_run_uncompiled_plan_fails(runner: CliRunner, tmp_path: Path) -> None:
    """dgov run should fail if the plan is not compiled (missing source_mtime_max)."""
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[plan]\nname = "uncompiled"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )
    result = runner.invoke(cli, ["run", str(plan)])
    assert result.exit_code != 0
    assert "not compiled" in result.output.lower()
    assert "_root.toml" in result.output


def test_run_compiled_plan_passes_check(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    """dgov run should pass the check if source_mtime_max is present (sop_set_hash not required)."""
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )

    # We mock the actual runner execution to just test the pre-run check
    # because full execution requires FIREWORKS_API_KEY and actual worktrees.
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", lambda *args, **kwargs: None)

    # We'll likely hit an error later when it tries to use the None runner,
    # but the point is it passed the "uncompiled" check.
    result = runner.invoke(cli, ["run", str(plan)])

    # It should NOT fail with "not compiled"
    assert "not compiled" not in result.output.lower()
