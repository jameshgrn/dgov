"""Tests for task persistence."""

import os
import tempfile

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
        worktree_path=os.path.join(tmp_project, "worktrees", "test-task-001"),
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
    assert retrieved["prompt"] == "Fix the bug in src/foo.py"
    assert retrieved["state"] == "active"
    assert retrieved.get("task_id") is None


def test_update_task_state(tmp_project, sample_task):
    """Can update task state with valid transitions."""
    tasks.add_task(tmp_project, sample_task)

    # ACTIVE -> DONE is valid
    tasks.update_task_state(tmp_project, "test-task-001", TaskState.DONE)
    retrieved = tasks.get_task(tmp_project, "test-task-001")
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
            worktree_path=os.path.join(tmp_project, f"worktrees/task-{i:03d}"),
            branch_name=f"task/task-{i:03d}",
            state=TaskState.ACTIVE,
        )
        tasks.add_task(tmp_project, task)

    results = tasks.get_tasks(tmp_project, ["task-001", "task-002"])
    assert len(results) == 2
    slugs = {r["slug"] for r in results}
    assert slugs == {"task-001", "task-002"}


def test_active_and_settled_tasks(tmp_project):
    """Can filter tasks by active vs settled state."""
    # Add active task
    active = WorkerTask(
        slug="active-task",
        prompt="Still working",
        agent="qwen",
        project_root=tmp_project,
        worktree_path=os.path.join(tmp_project, "worktrees/active"),
        branch_name="task/active",
        state=TaskState.ACTIVE,
    )
    tasks.add_task(tmp_project, active)

    # Add done task
    done = WorkerTask(
        slug="done-task",
        prompt="Finished",
        agent="qwen",
        project_root=tmp_project,
        worktree_path=os.path.join(tmp_project, "worktrees/done"),
        branch_name="task/done",
        state=TaskState.DONE,
    )
    tasks.add_task(tmp_project, done)

    active_list = tasks.active_tasks(tmp_project)
    assert len(active_list) == 1
    assert active_list[0]["slug"] == "active-task"

    settled = tasks.settled_tasks(tmp_project)
    assert len(settled) == 1
    assert settled[0]["slug"] == "done-task"


def test_count_active(tmp_project, sample_task):
    """Can count active tasks."""
    assert tasks.count_active(tmp_project) == 0

    tasks.add_task(tmp_project, sample_task)
    assert tasks.count_active(tmp_project) == 1

    tasks.update_task_state(tmp_project, "test-task-001", TaskState.DONE)
    assert tasks.count_active(tmp_project) == 0


def test_remove_task(tmp_project, sample_task):
    """Can remove a task and it records slug history."""
    tasks.add_task(tmp_project, sample_task)
    tasks.remove_task(tmp_project, "test-task-001")

    assert tasks.get_task(tmp_project, "test-task-001") is None
    assert "test-task-001" in tasks.get_slug_history(tmp_project)


def test_settle_completion_state(tmp_project, sample_task):
    """Settle completion state works with proper transitions between completion states."""
    tasks.add_task(tmp_project, sample_task)

    # First, transition to DONE via normal path (ACTIVE -> DONE is valid)
    tasks.update_task_state(tmp_project, "test-task-001", TaskState.DONE)

    # Then settle to FAILED (DONE -> FAILED is a valid completion state transition)
    result = tasks.settle_completion_state(tmp_project, "test-task-001", TaskState.FAILED)
    assert result.changed is True

    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved["state"] == "failed"


def test_replace_all_tasks(tmp_project):
    """Can replace all tasks at once."""
    # Add initial task
    task = WorkerTask(
        slug="original",
        prompt="Original",
        agent="qwen",
        project_root=tmp_project,
        worktree_path=os.path.join(tmp_project, "worktrees/original"),
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
            "worktree_path": os.path.join(tmp_project, "worktrees/r1"),
            "branch_name": "task/r1",
            "state": TaskState.ACTIVE,
            "task_id": None,
        },
        {
            "slug": "replaced-2",
            "prompt": "Replaced 2",
            "agent": "claude",
            "project_root": tmp_project,
            "worktree_path": os.path.join(tmp_project, "worktrees/r2"),
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


def test_set_task_metadata(tmp_project, sample_task):
    """Can set typed metadata on a task."""
    tasks.add_task(tmp_project, sample_task)

    tasks.set_task_metadata(
        tmp_project, "test-task-001", file_claims=["src/foo.py"], commit_message="Fix foo"
    )

    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved["file_claims"] == ["src/foo.py"]
    assert retrieved["commit_message"] == "Fix foo"


def test_update_file_claims(tmp_project, sample_task):
    """Can update file claims."""
    tasks.add_task(tmp_project, sample_task)
    tasks.update_file_claims(tmp_project, "test-task-001", ["a.py", "b.py"])

    retrieved = tasks.get_task(tmp_project, "test-task-001")
    assert retrieved["file_claims"] == ["a.py", "b.py"]
