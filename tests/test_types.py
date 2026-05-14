"""Tests for dgov.types module.

Covers the legacy core types `TaskState`, `WorkerExit`, and `Worktree`.
The `DispatchRun`, `DispatchRunState`, and `WatermasterId` names that
`types.py` re-exports from `dgov.dispatch_run` (Brief 9, 2026-05-14) have
their own dedicated test module at `tests/test_dispatch_run.py`.
"""

from pathlib import Path

from dgov.types import TaskState, WorkerExit, Worktree


class TestTaskState:
    def test_active_value(self):
        assert TaskState.ACTIVE == "active"

    def test_all_states_are_strings(self):
        for state in TaskState:
            assert isinstance(state, str)


class TestWorkerExit:
    def test_frozen(self):
        we = WorkerExit(task_slug="a", pane_slug="p", exit_code=0, output_dir="/tmp")
        assert we.task_slug == "a"
        assert we.exit_code == 0


class TestWorktree:
    def test_named_tuple(self):
        wt = Worktree(path=Path("/tmp/wt"), branch="dgov/a", commit="abc123")
        assert wt.path == Path("/tmp/wt")
        assert wt.branch == "dgov/a"
