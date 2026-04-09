"""Unit tests for the `dgov clean` CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import add_task
from dgov.persistence.schema import WorkerTask
from dgov.types import TaskState

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_clean_removes_runtime_fix_plan_artifacts_without_touching_authored_plans(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    authored_plan_dir = tmp_path / ".dgov" / "plans" / "keep-me"
    authored_plan_dir.mkdir(parents=True)
    (authored_plan_dir / "_root.toml").write_text('[plan]\nname = "keep-me"\n')

    runtime_fix_dir = tmp_path / ".dgov" / "runtime" / "fix-plans" / "stale-fix"
    runtime_fix_dir.mkdir(parents=True)
    (runtime_fix_dir / "_root.toml").write_text('[plan]\nname = "stale-fix"\n')

    runtime_archive_dir = tmp_path / ".dgov" / "runtime" / "fix-plans" / "archive" / "old-fix"
    runtime_archive_dir.mkdir(parents=True)
    (runtime_archive_dir / "_root.toml").write_text('[plan]\nname = "old-fix"\n')

    result = runner.invoke(cli, ["clean"])

    assert result.exit_code == 0, result.output
    assert not runtime_fix_dir.exists()
    assert not runtime_archive_dir.exists()
    assert authored_plan_dir.exists()


def test_clean_preserves_active_runtime_fix_plan(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    live_plan_dir = tmp_path / ".dgov" / "runtime" / "fix-plans" / "fix-live"
    live_plan_dir.mkdir(parents=True)
    (live_plan_dir / "_root.toml").write_text('[plan]\nname = "fix-live"\n')

    stale_plan_dir = tmp_path / ".dgov" / "runtime" / "fix-plans" / "fix-stale"
    stale_plan_dir.mkdir(parents=True)
    (stale_plan_dir / "_root.toml").write_text('[plan]\nname = "fix-stale"\n')

    add_task(
        str(tmp_path),
        WorkerTask(
            slug="fix/main.apply",
            prompt="test",
            agent="test",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / ".dgov" / "worktrees" / "fix-live"),
            branch_name="fix-live-branch",
            state=TaskState.ACTIVE,
            plan_name="fix-live",
        ),
    )

    result = runner.invoke(cli, ["clean"])

    assert result.exit_code == 0, result.output
    assert live_plan_dir.exists()
    assert not stale_plan_dir.exists()
    assert "Preserved (active)" in result.output
