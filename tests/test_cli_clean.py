"""Unit tests for the `dgov clean` CLI command."""

from __future__ import annotations

import subprocess
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


def _init_git_repo(path: Path) -> None:
    """Initialize a fresh git repo with one empty commit."""
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "init", str(path)], env=env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )


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


def test_clean_prunes_orphan_worktree_directory(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dgov clean should remove a stale sibling worktree directory."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _init_git_repo(project_root)
    monkeypatch.chdir(project_root)

    # Simulate a crashed-session artifact: a sibling worktrees dir with a
    # stale subdirectory git doesn't track.
    worktrees_dir = tmp_path / ".dgov-worktrees-proj"
    worktrees_dir.mkdir()
    orphan = worktrees_dir / "crashed-task"
    orphan.mkdir()
    (orphan / "leftover.txt").write_text("debris\n")

    result = runner.invoke(cli, ["clean"])

    assert result.exit_code == 0, result.output
    assert not orphan.exists()
    assert "Pruned 1 orphan worktree" in result.output


def test_clean_dry_run_does_not_touch_orphan_worktree(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dgov clean --dry-run should report but not remove orphan worktrees."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _init_git_repo(project_root)
    monkeypatch.chdir(project_root)

    worktrees_dir = tmp_path / ".dgov-worktrees-proj"
    worktrees_dir.mkdir()
    orphan = worktrees_dir / "crashed-task"
    orphan.mkdir()

    result = runner.invoke(cli, ["clean", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert orphan.exists()
    assert "Would prune 1 orphan worktree" in result.output
    assert "Dry run complete" in result.output
