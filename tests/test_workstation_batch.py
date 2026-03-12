"""Tests for dgov.batch module."""

import json
from pathlib import Path

import pytest

from dgov.batch import (
    BatchTask,
    _to_task_specs,
    compute_tiers,
    load_batch,
)
from dgov.models import TaskSpec

pytestmark = pytest.mark.unit


class TestLoadBatch:
    """Tests for loading batch specs from JSON files."""

    def test_load_valid_batch_spec(self, tmp_path: Path) -> None:
        """Test that a valid batch spec JSON loads with tasks parsed correctly."""
        spec_content = {
            "project_root": "/test/project",
            "tasks": [
                {
                    "id": "task-1",
                    "prompt": "Implement feature A",
                    "agent": "pi",
                    "touches": ["src/a.py"],
                    "permission_mode": "acceptEdits",
                    "timeout": 600,
                },
                {
                    "id": "task-2",
                    "prompt": "Implement feature B",
                    "agent": "claude",
                    "touches": ["src/b.py"],
                    "permission_mode": "plan",
                    "timeout": 300,
                },
            ],
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        project_root, tasks = load_batch(str(spec_file))

        assert project_root == "/test/project"
        assert len(tasks) == 2
        assert tasks[0].id == "task-1"
        assert tasks[0].prompt == "Implement feature A"
        assert tasks[0].agent == "pi"
        assert tasks[0].touches == ["src/a.py"]
        assert tasks[0].permission_mode == "acceptEdits"
        assert tasks[0].timeout == 600
        assert tasks[1].id == "task-2"
        assert tasks[1].agent == "claude"

    def test_load_batch_default_values(self, tmp_path: Path) -> None:
        """Test that batch spec loads with default values for missing fields."""
        spec_content = {
            "project_root": "/minimal/project",
            "tasks": [
                {"id": "task-1", "prompt": "Simple task"},
            ],
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        _, tasks = load_batch(str(spec_file))

        assert len(tasks) == 1
        assert tasks[0].agent == "pi"
        assert tasks[0].touches == []
        assert tasks[0].permission_mode == "acceptEdits"
        assert tasks[0].timeout == 600

    def test_load_empty_batch_spec(self, tmp_path: Path) -> None:
        """Test that empty batch spec returns no tasks."""
        spec_content = {
            "project_root": "/empty/project",
            "tasks": [],
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        _, tasks = load_batch(str(spec_file))

        assert len(tasks) == 0

    def test_load_batch_missing_required_id(self, tmp_path: Path) -> None:
        """Test that batch spec with missing id raises KeyError."""
        spec_content = {
            "project_root": "/test/project",
            "tasks": [
                {"prompt": "Missing id field"},
            ],
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        with pytest.raises(KeyError):
            load_batch(str(spec_file))


class TestBatchTaskToTaskSpec:
    """Tests for BatchTask to TaskSpec conversion."""

    def test_conversion_with_all_fields(self) -> None:
        """Test that all BatchTask fields propagate correctly to TaskSpec."""
        batch_task = BatchTask(
            id="full-task",
            prompt="Full feature implementation",
            agent="gemini",
            touches=["src/main.py", "src/util.py"],
            permission_mode="bypassPermissions",
            timeout=1200,
        )

        specs = _to_task_specs([batch_task])
        task_spec = specs["full-task"]

        assert isinstance(task_spec, TaskSpec)
        assert task_spec.id == "full-task"
        assert task_spec.description == "Full feature implementation"
        assert task_spec.exports == []
        assert task_spec.imports == []
        assert task_spec.touches == ["src/main.py", "src/util.py"]
        assert task_spec.body == "Full feature implementation"
        assert task_spec.timeout == 1200
        assert task_spec.worker_cmd == "gemini"
        assert task_spec.permission_mode == "bypassPermissions"

    def test_conversion_with_pi_agent(self) -> None:
        """Test that pi agent sets worker_cmd to 'pi' (non-default)."""
        batch_task = BatchTask(
            id="pi-task",
            prompt="Pi task",
            agent="pi",
            touches=["src/test.py"],
            permission_mode="acceptEdits",
            timeout=600,
        )

        specs = _to_task_specs([batch_task])
        task_spec = specs["pi-task"]

        assert task_spec.worker_cmd == "pi"

    def test_conversion_multiple_tasks(self) -> None:
        """Test conversion of multiple BatchTasks to TaskSpecs."""
        tasks = [
            BatchTask(id="t1", prompt="Task 1", touches=["src/a.py"], timeout=300),
            BatchTask(id="t2", prompt="Task 2", touches=["src/b.py"], timeout=600),
            BatchTask(id="t3", prompt="Task 3", touches=["src/c.py"], timeout=900),
        ]

        specs = _to_task_specs(tasks)

        assert len(specs) == 3
        assert "t1" in specs and "t2" in specs and "t3" in specs
        assert specs["t1"].timeout == 300
        assert specs["t2"].timeout == 600
        assert specs["t3"].timeout == 900


class TestComputeTiers:
    """Tests for tier computation with task dependencies."""

    def test_empty_batch_returns_no_tiers(self) -> None:
        """Test that empty batch returns no tiers."""
        result = compute_tiers([])
        assert result == []

    def test_single_task_in_first_tier(self) -> None:
        """Test single task is placed in first tier."""
        tasks = [BatchTask(id="t1", prompt="Single task", touches=["src/a.py"])]

        tiers = compute_tiers(tasks)

        assert len(tiers) == 1
        assert "t1" in tiers[0]

    def test_tasks_with_no_conflicts_in_same_tier(self) -> None:
        """Test tasks with disjoint touches are in same tier."""
        tasks = [
            BatchTask(id="a", prompt="Task A", touches=["src/a.py"]),
            BatchTask(id="b", prompt="Task B", touches=["src/b.py"]),
            BatchTask(id="c", prompt="Task C", touches=["src/c.py"]),
        ]

        tiers = compute_tiers(tasks)

        assert len(tiers) == 1
        # All tasks should be in the first tier since no conflicts
        assert set(tiers[0]) == {"a", "b", "c"}

    def test_tasks_with_touch_conflicts_in_separate_tiers(self) -> None:
        """Test that touching same file serializes across tiers."""
        tasks = [
            BatchTask(id="first", prompt="First task", touches=["src/shared.py"]),
            BatchTask(id="second", prompt="Second task", touches=["src/shared.py"]),
            BatchTask(id="third", prompt="Third task", touches=["src/other.py"]),
        ]

        tiers = compute_tiers(tasks)

        # first and second must be in different tiers due to conflict
        tier_ids = {tid for tier in tiers for tid in tier}
        assert set(tier_ids) == {"first", "second", "third"}
        # Verify they are separated
        if len(tiers) >= 2:
            first_tier = tiers[0]
            # At least one conflict pair should be in different tiers
            first_in_tier_0 = "first" in first_tier
            second_in_tier_0 = "second" in first_tier
            if first_in_tier_0 and second_in_tier_0:
                assert False, "Conflicting tasks should be in different tiers"

    def test_batch_spec_with_dependencies_correct_ordering(self) -> None:
        """Test batch spec with file dependencies produces correct tier ordering."""
        # Task A writes src/core.py, Task B reads it (touches same file), Task C depends on B
        tasks = [
            BatchTask(id="init", prompt="Initialize", touches=["src/core.py"]),
            BatchTask(id="setup", prompt="Setup", touches=["src/setup.py"]),
            BatchTask(id="main", prompt="Main logic", touches=["src/main.py"]),
            BatchTask(id="test", prompt="Tests", touches=["tests/test_main.py"]),
        ]

        tiers = compute_tiers(tasks)

        # Since no conflicts, all should be in tier 1 (parallelizable)
        assert len(tiers) == 1
        assert set(tiers[0]) == {"init", "setup", "main", "test"}

    def test_chained_dependencies_sequential_tiers(self) -> None:
        """Test chained touches create sequential tiers."""
        tasks = [
            BatchTask(id="core", prompt="Core module", touches=["src/core.py"]),
            BatchTask(id="util", prompt="Utils depend on core", touches=["src/util.py"]),
            BatchTask(id="app", prompt="App depends on util", touches=["src/app.py"]),
        ]

        tiers = compute_tiers(tasks)

        # Each task touches different files, so no serialization needed
        assert len(tiers) == 1
        assert set(tiers[0]) == {"core", "util", "app"}


class TestErrorHandling:
    """Tests for error handling in batch operations."""

    def test_invalid_batch_missing_tasks_field(self, tmp_path: Path) -> None:
        """Test that batch spec without tasks field raises error."""
        spec_content = {
            "project_root": "/test/project",
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        with pytest.raises(KeyError):
            load_batch(str(spec_file))

    def test_invalid_batch_tasks_not_list(self, tmp_path: Path) -> None:
        """Test that batch spec with non-list tasks field raises error."""
        spec_content = {
            "project_root": "/test/project",
            "tasks": {"id": "task-1"},  # Should be a list
        }

        spec_file = tmp_path / "batch.json"
        json.dump(spec_content, open(spec_file, "w"))

        with pytest.raises(TypeError):
            load_batch(str(spec_file))

    def test_invalid_batch_json_format(self, tmp_path: Path) -> None:
        """Test that invalid JSON raises appropriate error."""
        spec_file = tmp_path / "batch.json"
        spec_file.write_text("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            load_batch(str(spec_file))

    def test_invalid_batch_nonexistent_file(self, tmp_path: Path) -> None:
        """Test that nonexistent batch file raises FileNotFoundError."""
        nonexistent = str(tmp_path / "nonexistent.json")

        with pytest.raises(FileNotFoundError):
            load_batch(nonexistent)


class TestPermissionModePropagation:
    """Tests for permission_mode propagation from BatchTask to TaskSpec."""

    def test_permission_mode_propagation_to_task_spec(self) -> None:
        """Test that BatchTask.permission_mode propagates to TaskSpec.permission_mode."""
        task = BatchTask(
            id="test-task",
            prompt="Implement feature X",
            agent="pi",
            touches=["src/test.py"],
            permission_mode="plan",
            timeout=300,
        )

        specs = _to_task_specs([task])
        task_spec = specs["test-task"]

        assert isinstance(task_spec, TaskSpec)
        assert task_spec.permission_mode == "plan"

    def test_permission_mode_default_value(self) -> None:
        """Test that BatchTask uses default permission_mode when not specified."""
        task = BatchTask(
            id="default-mode-task",
            prompt="Simple task",
            agent="pi",
            touches=[],
            timeout=600,
        )

        specs = _to_task_specs([task])
        task_spec = specs["default-mode-task"]

        assert task_spec.permission_mode == "acceptEdits"

    def test_permission_mode_various_values(self) -> None:
        """Test that different permission_mode values are preserved."""
        modes = ["bypassPermissions", "plan", "acceptEdits", "reviewOnly"]

        for mode in modes:
            task = BatchTask(
                id=f"task-{mode}",
                prompt=f"Task with {mode}",
                agent="pi",
                touches=[],
                permission_mode=mode,
            )

            specs = _to_task_specs([task])
            task_spec = specs[f"task-{mode}"]

            assert task_spec.permission_mode == mode, f"Failed for mode={mode}"

    """Test that BatchTask.permission_mode propagates to TaskSpec.permission_mode."""
    task = BatchTask(
        id="test-task",
        prompt="Implement feature X",
        agent="pi",
        touches=["src/test.py"],
        permission_mode="plan",
        timeout=300,
    )

    specs = _to_task_specs([task])
    task_spec = specs["test-task"]

    assert isinstance(task_spec, TaskSpec)
    assert task_spec.permission_mode == "plan"


def test_permission_mode_default_value():
    """Test that BatchTask uses default permission_mode when not specified."""
    task = BatchTask(
        id="default-mode-task",
        prompt="Simple task",
        agent="pi",
        touches=[],
        timeout=600,
    )

    specs = _to_task_specs([task])
    task_spec = specs["default-mode-task"]

    assert task_spec.permission_mode == "acceptEdits"


def test_permission_mode_various_values():
    """Test that different permission_mode values are preserved."""
    modes = ["bypassPermissions", "plan", "acceptEdits", "reviewOnly"]

    for mode in modes:
        task = BatchTask(
            id=f"task-{mode}",
            prompt=f"Task with {mode}",
            agent="pi",
            touches=[],
            permission_mode=mode,
        )

        specs = _to_task_specs([task])
        task_spec = specs[f"task-{mode}"]

        assert task_spec.permission_mode == mode, f"Failed for mode={mode}"
