"""Tests for dgov.dag_parser module."""

from pathlib import Path

import pytest

from dgov.dag_parser import (
    DagDefinition,
    DagFileSpec,
    DagTaskSpec,
    parse_dag_file,
)

# =============================================================================
# Tests for parse_dag_file()
# =============================================================================

VALID_DAG_TOML = """
[dag]
name = "test-dag"
project_root = "./src"
session_root = "."
max_concurrent = 2

[tasks.init]
summary = "Initialize project"
prompt = "Run init"
commit_message = "init commit"

[tasks.init.files]
create = ["config.json"]

[tasks.build]
summary = "Build project"
prompt = "Run build"
commit_message = "build"
depends_on = ["init"]

[tasks.build.files]
edit = ["src/main.py"]
"""


def test_parse_dag_file_valid_toml(tmp_path: Path):
    """Test parse_dag_file() with valid TOML input."""
    dag_file = tmp_path / "test.dag.toml"
    dag_file.write_text(VALID_DAG_TOML)

    result = parse_dag_file(str(dag_file))

    assert isinstance(result, DagDefinition)
    assert result.name == "test-dag"
    assert result.project_root == "./src"
    assert result.max_concurrent == 2
    assert len(result.tasks) == 2

    # Check task slugs are correctly assigned
    assert "init" in result.tasks
    assert "build" in result.tasks

    # Check task properties
    init_task = result.tasks["init"]
    assert init_task.slug == "init"
    assert init_task.summary == "Initialize project"
    assert init_task.files.create == ("config.json",)

    build_task = result.tasks["build"]
    assert build_task.slug == "build"
    assert build_task.depends_on == ("init",)
    assert build_task.files.edit == ("src/main.py",)


def test_parse_dag_file_missing_dag_section(tmp_path: Path):
    """Test parse_dag_file() with missing [dag] section raises ValueError."""
    toml_content = """
[tasks.test]
summary = "Test task"
prompt = "Run test"
commit_message = "test"
"""
    dag_file = tmp_path / "no_dag.dag.toml"
    dag_file.write_text(toml_content)

    with pytest.raises(ValueError, match="Missing \\[plan\\] or \\[dag\\] section"):
        parse_dag_file(str(dag_file))


def test_parse_dag_file_missing_tasks_section(tmp_path: Path):
    """Test parse_dag_file() with missing [tasks] section raises ValueError."""
    toml_content = """
[dag]
name = "no-tasks-dag"
"""
    dag_file = tmp_path / "no_tasks.dag.toml"
    dag_file.write_text(toml_content)

    with pytest.raises(ValueError, match="Missing \\[units\\] or \\[tasks\\] section"):
        parse_dag_file(str(dag_file))


def test_parse_dag_file_nonexistent_file():
    """Test parse_dag_file() with nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Plan file not found"):
        parse_dag_file("/nonexistent/path/to/file.dag.toml")


# =============================================================================
# Tests for DagTaskSpec.all_touches()
# =============================================================================


def test_all_touches_returns_deduplicated_union():
    """Test all_touches() returns deduplicated union of create+edit+delete."""
    files = DagFileSpec(
        create=("file1.txt", "file2.txt", "shared.py"),
        edit=("file3.txt", "shared.py"),
        delete=("file4.txt",),
    )

    task = DagTaskSpec(
        slug="test-task",
        summary="Test task",
        prompt="Run this test",
        commit_message="test commit",
        files=files,
    )

    result = task.all_touches()

    # shared.py appears in both create and edit - should be deduplicated
    assert result == (
        "file1.txt",
        "file2.txt",
        "shared.py",
        "file3.txt",
        "file4.txt",
    )


def test_all_touches_empty_files():
    """Test all_touches() with empty file lists."""
    task = DagTaskSpec(
        slug="empty-task",
        summary="Empty task",
        prompt="No files",
        commit_message="empty commit",
    )

    result = task.all_touches()
    assert result == ()


def test_all_touches_partial_files():
    """Test all_touches() with only some file operations."""
    files = DagFileSpec(
        create=("created.txt",),
        edit=(),
        delete=(),
    )

    task = DagTaskSpec(
        slug="partial-task",
        summary="Partial task",
        prompt="Partial files",
        commit_message="partial",
        files=files,
    )

    result = task.all_touches()
    assert result == ("created.txt",)


# =============================================================================
# Tests for DagFileSpec defaults
# =============================================================================


def test_dag_file_spec_defaults_to_empty_tuples():
    """Test DagFileSpec defaults all fields to empty tuples."""
    spec = DagFileSpec()

    assert spec.create == ()
    assert spec.edit == ()
    assert spec.delete == ()


def test_dag_file_spec_partial_defaults():
    """Test DagFileSpec with partial explicit values, rest defaults."""
    spec = DagFileSpec(create=("file.txt",))

    assert spec.create == ("file.txt",)
    assert spec.edit == ()
    assert spec.delete == ()


# =============================================================================
# Additional integration tests
# =============================================================================


def test_parse_dag_file_with_alternative_plan_naming(tmp_path: Path):
    """Test parse_dag_file() supports [plan] instead of [dag]."""
    toml_content = """
[plan]
name = "plan-named-dag"

[tasks.task1]
summary = "Task 1"
prompt = "Do task 1"
commit_message = "task 1"
"""
    dag_file = tmp_path / "plan_named.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    assert result.name == "plan-named-dag"


def test_parse_dag_file_with_alternative_units_naming(tmp_path: Path):
    """Test parse_dag_file() supports [units] instead of [tasks]."""
    toml_content = """
[dag]
name = "units-dag"

[units.unit1]
summary = "Unit 1"
prompt = "Do unit 1"
commit_message = "unit 1"
"""
    dag_file = tmp_path / "units_named.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    assert "unit1" in result.tasks


def test_parse_dag_file_with_acceptance_criteria(tmp_path: Path):
    """Test parse_dag_file() with acceptance criteria flattening."""
    toml_content = """
[dag]
name = "acceptance-dag"

[tasks.test]
summary = "Test task"
prompt = "Test it"
commit_message = "test"

[tasks.test.acceptance]
tests_pass = false
lint_clean = true
post_merge_check = "verify.sh"
"""
    dag_file = tmp_path / "acceptance.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    task = result.tasks["test"]

    # Acceptance fields should be flattened into task
    assert task.tests_pass is False
    assert task.lint_clean is True
    assert task.post_merge_check == "verify.sh"


def test_parse_dag_file_with_evals(tmp_path: Path):
    """Test parse_dag_file() with evals section."""
    toml_content = """
[dag]
name = "evals-dag"

[tasks.task1]
summary = "Task 1"
prompt = "Do it"
commit_message = "task 1"

[[evals]]
id = "eval-1"
kind = "unit"
statement = "All tests pass"
evidence = "pytest output"
"""
    dag_file = tmp_path / "evals.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    assert len(result.evals) == 1
    assert result.evals[0].id == "eval-1"
    assert result.evals[0].statement == "All tests pass"


def test_parse_dag_file_with_default_values(tmp_path: Path):
    """Test parse_dag_file() applies default values correctly."""
    toml_content = """
[dag]
name = "minimal-dag"

[tasks.task1]
summary = "Task 1"
prompt = "Do it"
commit_message = "task 1"
"""
    dag_file = tmp_path / "defaults.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))

    # Check DagDefinition defaults
    assert result.project_root == "."
    assert result.session_root == "."
    assert result.max_concurrent == 0
    assert result.default_max_retries == 3
    assert result.merge_resolve == "skip"
    assert result.merge_squash is True

    # Check DagTaskSpec defaults
    task = result.tasks["task1"]
    assert task.agent == "worker"
    assert task.escalation == ()
    assert task.depends_on == ()
    assert task.timeout_s == 900
    assert task.permission_mode == "bypassPermissions"
    assert task.tests_pass is True
    assert task.lint_clean is True
    assert task.role == "worker"
