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
[plan]
name = "test-dag"
project_root = "./src"
session_root = "."

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
    assert len(result.tasks) == 2

    assert "init" in result.tasks
    assert "build" in result.tasks

    init_task = result.tasks["init"]
    assert init_task.slug == "init"
    assert init_task.summary == "Initialize project"
    assert init_task.files.create == ("config.json",)

    build_task = result.tasks["build"]
    assert build_task.slug == "build"
    assert build_task.depends_on == ("init",)
    assert build_task.files.edit == ("src/main.py",)


def test_parse_dag_file_missing_plan_section(tmp_path: Path):
    """Test parse_dag_file() with missing [plan] section raises ValueError."""
    toml_content = """
[tasks.test]
summary = "Test task"
prompt = "Run test"
commit_message = "test"
"""
    dag_file = tmp_path / "no_plan.dag.toml"
    dag_file.write_text(toml_content)

    with pytest.raises(ValueError, match="Missing \\[plan\\] section"):
        parse_dag_file(str(dag_file))


def test_parse_dag_file_missing_tasks_section(tmp_path: Path):
    """Test parse_dag_file() with missing [tasks] section raises ValueError."""
    toml_content = """
[plan]
name = "no-tasks-dag"
"""
    dag_file = tmp_path / "no_tasks.dag.toml"
    dag_file.write_text(toml_content)

    with pytest.raises(ValueError, match="Missing \\[tasks\\] section"):
        parse_dag_file(str(dag_file))


def test_parse_dag_file_nonexistent_file():
    """Test parse_dag_file() with nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Plan file not found"):
        parse_dag_file("/nonexistent/path/to/file.dag.toml")


def test_parse_dag_file_rejects_unknown_fields(tmp_path: Path):
    """extra='forbid' catches typos like deps instead of depends_on."""
    toml_content = """
[plan]
name = "typo-dag"

[tasks.task1]
summary = "Task 1"
prompt = "Do it"
commit_message = "task 1"
deps = ["other"]
"""
    dag_file = tmp_path / "typo.dag.toml"
    dag_file.write_text(toml_content)

    with pytest.raises(Exception):
        parse_dag_file(str(dag_file))


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
    assert spec.touch == ()


def test_dag_file_spec_partial_defaults():
    """Test DagFileSpec with partial explicit values, rest defaults."""
    spec = DagFileSpec(create=("file.txt",))

    assert spec.create == ("file.txt",)
    assert spec.edit == ()
    assert spec.delete == ()
    assert spec.touch == ()


def test_dag_file_spec_touch_field():
    """Test DagFileSpec accepts touch field."""
    spec = DagFileSpec(touch=("a.py", "b.py"))
    assert spec.touch == ("a.py", "b.py")
    assert spec.create == ()


# =============================================================================
# Tests for flat files shorthand (files = [...])
# =============================================================================


def test_parse_dag_file_flat_files_list(tmp_path: Path):
    """Test parse_dag_file() with flat files = [...] shorthand."""
    toml_content = """
[plan]
name = "flat-files-dag"

[tasks.task1]
summary = "Task with flat files"
prompt = "Do it"
commit_message = "done"
files = ["src/foo.py", "tests/test_foo.py"]
"""
    dag_file = tmp_path / "flat.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    task = result.tasks["task1"]
    assert task.files.touch == ("src/foo.py", "tests/test_foo.py")
    assert task.files.create == ()
    assert task.files.edit == ()


def test_parse_dag_file_flat_files_in_all_touches(tmp_path: Path):
    """Flat files list appears in all_touches()."""
    toml_content = """
[plan]
name = "touch-dag"

[tasks.task1]
summary = "Task"
prompt = "Do"
commit_message = "d"
files = ["a.py", "b.py"]
"""
    dag_file = tmp_path / "touch.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))
    assert result.tasks["task1"].all_touches() == ("a.py", "b.py")


def test_all_touches_includes_touch_field():
    """all_touches() includes touch alongside create/edit/delete."""
    files = DagFileSpec(
        create=("new.py",),
        edit=("existing.py",),
        touch=("touched.py",),
    )
    task = DagTaskSpec(
        slug="t",
        summary="s",
        prompt="p",
        commit_message="c",
        files=files,
    )
    result = task.all_touches()
    assert "touched.py" in result
    assert "new.py" in result
    assert "existing.py" in result


def test_all_touches_deduplicates_touch_with_edit():
    """Touch and edit listing the same file is deduplicated."""
    files = DagFileSpec(edit=("same.py",), touch=("same.py",))
    task = DagTaskSpec(slug="t", summary="s", prompt="p", commit_message="c", files=files)
    assert task.all_touches() == ("same.py",)


# =============================================================================
# Tests for default values
# =============================================================================


def test_parse_dag_file_with_default_values(tmp_path: Path):
    """Test parse_dag_file() applies default values correctly."""
    toml_content = """
[plan]
name = "minimal-dag"

[tasks.task1]
summary = "Task 1"
prompt = "Do it"
commit_message = "task 1"
"""
    dag_file = tmp_path / "defaults.dag.toml"
    dag_file.write_text(toml_content)

    result = parse_dag_file(str(dag_file))

    assert result.project_root == "."
    assert result.session_root == "."
    assert result.default_max_retries == 3

    task = result.tasks["task1"]
    assert task.agent == ""
    assert task.depends_on == ()
    assert task.timeout_s == 900
