"""Tests for manual state repair CLI commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import compile_plan_tree

from dgov.cli import cli
from dgov.deploy_log import read as read_deploy_log
from dgov.persistence import add_task, get_task, read_events
from dgov.persistence.schema import WorkerTask
from dgov.types import TaskState

pytestmark = pytest.mark.unit

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.local",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.local",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
    )


def _init_git_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    (root / "README.md").write_text("init\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_retry_resets_failed_task_and_reruns_only_that_unit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _init_git_repo(tmp_path)
    compiled_path = compile_plan_tree(
        tmp_path,
        "repair-plan",
        """
[tasks.alpha]
summary = "Alpha"
prompt = "Do alpha"
commit_message = "feat: alpha"
files.edit = ["src/alpha.py"]
""",
    )

    add_task(
        str(tmp_path),
        WorkerTask(
            slug="tasks/main.alpha",
            prompt="Do alpha",
            agent="test-agent",
            project_root=str(tmp_path),
            worktree_path="",
            branch_name="",
            state=TaskState.FAILED,
            plan_name="repair-plan",
        ),
    )

    captured: dict[str, object] = {}

    def _capture_run(plan_file: str, project_root: str, **kwargs: object) -> None:
        captured["plan_file"] = plan_file
        captured["project_root"] = project_root
        captured["kwargs"] = kwargs

    monkeypatch.setattr("dgov.cli.state._cmd_run_plan", _capture_run)

    result = runner.invoke(cli, ["retry", "tasks/main.alpha"])

    assert result.exit_code == 0, result.output
    compiled_path = compiled_path.resolve()
    assert captured["plan_file"] == str(compiled_path)
    assert captured["project_root"] == str(tmp_path)
    assert captured["kwargs"] == {
        "only": "tasks/main.alpha",
        "plan_dir": compiled_path.parent,
    }
    assert get_task(str(tmp_path), "tasks/main.alpha") is None
    assert read_events(str(tmp_path), task_slug="tasks/main.alpha") == []


def test_mark_done_recreates_merged_state_without_existing_db_row(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _init_git_repo(tmp_path)
    compile_plan_tree(
        tmp_path,
        "repair-plan",
        """
[tasks.alpha]
summary = "Alpha"
prompt = "Do alpha"
commit_message = "feat: alpha"
files.edit = ["src/alpha.py"]
""",
    )

    result = runner.invoke(cli, ["mark-done", "tasks/main.alpha"])

    assert result.exit_code == 0, result.output
    task = get_task(str(tmp_path), "tasks/main.alpha")
    assert task is not None
    assert task["state"] == TaskState.MERGED
    events = read_events(str(tmp_path), task_slug="tasks/main.alpha")
    assert [event["event"] for event in events] == [
        "dag_task_dispatched",
        "task_done",
        "review_pass",
        "merge_completed",
    ]
    deployed = read_deploy_log(str(tmp_path), "repair-plan")
    assert len(deployed) == 1
    assert deployed[0].unit == "tasks/main.alpha"
