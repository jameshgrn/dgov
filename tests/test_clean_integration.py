"""Integration tests for `dgov clean` against real runner artifacts."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.runner import EventDagRunner
from dgov.settlement import GateResult

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _env_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key-fake")


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    env = {
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env={**env, "PATH": os.environ["PATH"]},
            check=True,
            capture_output=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.name", "test")
    _git("config", "user.email", "test@test.local")
    (tmp_path / "README.md").write_text("init\n")
    _git("add", ".")
    _git("commit", "-m", "initial commit")
    return tmp_path


def _dag(task_slug: str) -> DagDefinition:
    return DagDefinition(
        name="clean-preserved-worktree",
        dag_file="test",
        tasks={
            task_slug: DagTaskSpec(
                slug=task_slug,
                summary=task_slug,
                prompt=task_slug,
                commit_message=f"feat: {task_slug}",
                agent="mock",
                files=DagFileSpec(),
            )
        },
    )


async def _mock_worker_ok(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    worktree_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
) -> None:
    (worktree_path / f"{task_slug}.txt").write_text(f"output from {task_slug}\n")
    on_exit(task_slug, pane_slug, 0, "")


def test_clean_preserves_rejected_inspection_worktree(
    runner: CliRunner,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """clean must not remove a preserved rejected worktree that git still tracks."""
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
    monkeypatch.setattr(
        "dgov.runner.validate_sandbox",
        lambda *_args, **_kwargs: GateResult(passed=False, error="lint failure"),
    )

    task_slug = "inspect-keep"
    dag = _dag(task_slug)
    results = asyncio.run(EventDagRunner(dag, session_root=str(git_repo)).run())

    assert results[task_slug] == "failed"
    worktrees_dir = git_repo.parent / f".dgov-worktrees-{git_repo.name}"
    preserved = worktrees_dir / task_slug
    assert preserved.exists(), "runner should preserve rejected worktree for inspection"

    result = runner.invoke(cli, ["clean"])

    assert result.exit_code == 0, result.output
    assert preserved.exists(), "clean must not delete preserved inspection worktree"
    assert "Pruned 0 orphan worktree" in result.output
