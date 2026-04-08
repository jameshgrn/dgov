"""Tests for dgov run --continue logic."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.runner import EventDagRunner
from dgov.settlement import GateResult


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Fresh git repo with one initial commit."""
    env = {
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

    def _git(*args: str):
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


def _dag(tasks: dict[str, DagTaskSpec], name: str = "test-continue") -> DagDefinition:
    return DagDefinition(
        name=name,
        dag_file="test",
        tasks=tasks,
    )


def _task(slug: str) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=slug,
        prompt=slug,
        commit_message=f"feat: {slug}",
        agent="mock",
        files=DagFileSpec(),
    )


async def _mock_worker_ok(
    project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
):
    out = worktree_path / f"{task_slug}.txt"
    out.write_text(f"output from {task_slug}\n")
    on_exit(task_slug, pane_slug, 0, "")


async def _mock_worker_fail(
    project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
):
    on_exit(task_slug, pane_slug, 1, "mock failure")


def test_continue_retries_failed_tasks(git_repo, monkeypatch):
    """Proves that --continue (continue_failed=True) picks up FAILED tasks."""

    async def _noop(self):
        pass

    monkeypatch.setattr("dgov.runner.EventDagRunner._preflight_check_models", _noop)

    dag = _dag({"t1": _task("t1")})
    session_root = str(git_repo)

    # 1. Run and fail
    print("\n--- Phase 1: Run and fail ---")
    monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
    runner1 = EventDagRunner(dag, session_root=session_root)
    results1 = asyncio.run(runner1.run())
    assert results1["t1"] == "failed"

    # 2. Run without continue -> should still be failed, no worker activity
    print("\n--- Phase 2: Run without continue ---")
    runner2 = EventDagRunner(dag, session_root=session_root)
    results2 = asyncio.run(runner2.run())
    assert results2["t1"] == "failed"

    # 3. Run with continue -> should retry and succeed if worker is now ok
    print("\n--- Phase 3: Run with continue ---")
    monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
    monkeypatch.setattr("dgov.runner.validate_sandbox", lambda *a, **k: GateResult(passed=True))

    runner3 = EventDagRunner(dag, session_root=session_root, continue_failed=True)
    results3 = asyncio.run(runner3.run())
    assert results3["t1"] == "merged"


def test_continue_retries_abandoned_tasks(git_repo, monkeypatch):
    """Proves that --continue (continue_failed=True) picks up ABANDONED tasks."""
    from dgov.persistence import emit_event
    from dgov.types import TaskState

    async def _noop(self):
        pass

    monkeypatch.setattr("dgov.runner.EventDagRunner._preflight_check_models", _noop)

    dag = _dag({"t1": _task("t1")}, name="test-abandoned")
    session_root = str(git_repo)

    # 1. Manually mark as DISPATCHED then ABANDONED in event log
    emit_event(
        session_root,
        "dag_task_dispatched",
        "t1-pane",
        plan_name=dag.name,
        task_slug="t1",
    )
    emit_event(
        session_root,
        "task_abandoned",
        "t1-pane",
        plan_name=dag.name,
        task_slug="t1",
    )

    # 2. Run without continue -> should rehydrate to ABANDONED and finish immediately
    runner1 = EventDagRunner(dag, session_root=session_root)
    assert runner1.kernel.task_states["t1"] == TaskState.ABANDONED
    results1 = asyncio.run(runner1.run())
    assert results1["t1"] == "abandoned"

    # 3. Run with continue -> should retry and succeed
    monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
    monkeypatch.setattr("dgov.runner.validate_sandbox", lambda *a, **k: GateResult(passed=True))

    runner2 = EventDagRunner(dag, session_root=session_root, continue_failed=True)
    assert runner2.kernel.task_states["t1"] == TaskState.PENDING
    results2 = asyncio.run(runner2.run())
    assert results2["t1"] == "merged"
