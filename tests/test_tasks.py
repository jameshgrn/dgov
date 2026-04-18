"""Tests for task persistence."""

import tempfile
from pathlib import Path

import pytest

from dgov.persistence import tasks
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.persistence.tasks import IllegalTransitionError


@pytest.fixture
def tmp_project():
    """Create a temporary project directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_task(tmp_project):
    """Create a sample task."""
    return WorkerTask(
        slug="test-task-001",
        prompt="Fix the bug in src/foo.py",
        agent="qwen",
        project_root=tmp_project,
        worktree_path=str(Path(tmp_project) / "worktrees" / "test-task-001"),
        branch_name="task/test-task-001",
        task_id=None,  # headless
        role="worker",
        state=TaskState.ACTIVE,
    )


def test_add_and_get_task(tmp_project, sample_task):
    """Can add a task and retrieve it."""
    tasks.add_task(tmp_project, sample_task)

    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved is not None
    assert retrieved["slug"] == "test-task-001"
    assert retrieved["state"] == "active"
    assert retrieved.get("task_id") is None


def test_update_task_state(tmp_project, sample_task):
    """Can update task state with valid transitions."""
    tasks.add_task(tmp_project, sample_task)

    # ACTIVE -> DONE is valid
    tasks.update_task_state(tmp_project, "test-task-001", TaskState.DONE)
    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved is not None
    assert retrieved["state"] == "done"


def test_illegal_transition(tmp_project, sample_task):
    """Illegal transitions raise an error."""
    tasks.add_task(tmp_project, sample_task)

    # ACTIVE -> MERGED is not allowed (must go through DONE and REVIEWED_PASS)
    with pytest.raises(IllegalTransitionError):
        tasks.update_task_state(tmp_project, "test-task-001", TaskState.MERGED)


def test_get_multiple_tasks(tmp_project):
    """Can retrieve multiple tasks by slug."""
    for i in range(3):
        task = WorkerTask(
            slug=f"task-{i:03d}",
            prompt=f"Task {i}",
            agent="qwen",
            project_root=tmp_project,
            worktree_path=str(Path(tmp_project) / f"worktrees/task-{i:03d}"),
            branch_name=f"task/task-{i:03d}",
            state=TaskState.ACTIVE,
        )
        tasks.add_task(tmp_project, task)

    results = tasks.get_tasks(tmp_project, ["task-001", "task-002"])
    assert len(results) == 2
    slugs = {r["slug"] for r in results}
    assert slugs == {"task-001", "task-002"}


def test_remove_task(tmp_project, sample_task):
    """Can remove a task and it records slug history."""
    tasks.add_task(tmp_project, sample_task)
    tasks.remove_task(tmp_project, "test-task-001")

    assert tasks.get_task(tmp_project, "test-task-001") is None
    assert "test-task-001" in tasks.get_slug_history(tmp_project)


def test_replace_all_tasks(tmp_project):
    """Can replace all tasks at once."""
    # Add initial task
    task = WorkerTask(
        slug="original",
        prompt="Original",
        agent="qwen",
        project_root=tmp_project,
        worktree_path=str(Path(tmp_project) / "worktrees/original"),
        branch_name="task/original",
        state=TaskState.ACTIVE,
    )
    tasks.add_task(tmp_project, task)

    # Replace with new set
    new_tasks = [
        {
            "slug": "replaced-1",
            "prompt": "Replaced 1",
            "agent": "qwen",
            "project_root": tmp_project,
            "worktree_path": str(Path(tmp_project) / "worktrees/r1"),
            "branch_name": "task/r1",
            "state": TaskState.ACTIVE,
            "task_id": None,
        },
        {
            "slug": "replaced-2",
            "prompt": "Replaced 2",
            "agent": "claude",
            "project_root": tmp_project,
            "worktree_path": str(Path(tmp_project) / "worktrees/r2"),
            "branch_name": "task/r2",
            "state": TaskState.DONE,
            "task_id": None,
        },
    ]
    tasks.replace_all_tasks(tmp_project, new_tasks)

    all_tasks = tasks.all_tasks(tmp_project)
    assert len(all_tasks) == 2
    slugs = {t["slug"] for t in all_tasks}
    assert slugs == {"replaced-1", "replaced-2"}


def test_set_task_metadata_plan_name_only(tmp_project, sample_task):
    """Task metadata persistence is limited to execution facts like plan identity."""
    tasks.add_task(tmp_project, sample_task)

    tasks.set_task_metadata(tmp_project, "test-task-001", plan_name="repair-plan")

    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved is not None
    assert retrieved["plan_name"] == "repair-plan"


def test_emit_event_none_kwargs_excluded(tmp_path, monkeypatch):
    """None kwargs should not appear as the string 'None' in events."""
    from dgov.persistence import clear_connection_cache, emit_event, read_events

    monkeypatch.setenv("HOME", str(tmp_path))
    clear_connection_cache()

    session_root = str(tmp_path)
    emit_event(session_root, "task_done", "test-pane", error=None, reason="ok")

    events = read_events(session_root)
    assert len(events) == 1
    ev = events[0]
    # error=None should be excluded, not stored as "None"
    assert ev.get("error") is None or ev.get("error") == ""
    # reason="ok" should still be stored
    assert ev.get("reason") == "ok"
