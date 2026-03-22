"""Tests for dgov.dag_parser — DAG TOML parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.dag_parser import DagDefinition, parse_dag_file

pytestmark = pytest.mark.unit

_VALID_TOML = """
[dag]
version = 1
name = "test-dag"

[tasks.task-a]
summary = "First task"
prompt = "Do task A"
commit_message = "Complete A"
agent = "qwen-35b"
files.edit = ["src/a.py"]

[tasks.task-b]
summary = "Second task"
prompt = "Do task B"
commit_message = "Complete B"
agent = "qwen-35b"
depends_on = ["task-a"]
files.create = ["src/b.py"]
"""


def _write_dag(tmp_path: Path, content: str = _VALID_TOML) -> str:
    p = tmp_path / "dag.toml"
    p.write_text(content)
    return str(p)


class TestParseDagFile:
    def test_valid_dag(self, tmp_path):
        defn = parse_dag_file(_write_dag(tmp_path))
        assert isinstance(defn, DagDefinition)
        assert defn.name == "test-dag"
        assert len(defn.tasks) == 2
        assert "task-a" in defn.tasks
        assert "task-b" in defn.tasks

    def test_task_fields(self, tmp_path):
        defn = parse_dag_file(_write_dag(tmp_path))
        task_a = defn.tasks["task-a"]
        assert task_a.slug == "task-a"
        assert task_a.summary == "First task"
        assert task_a.agent == "qwen-35b"
        assert task_a.files.edit == ("src/a.py",)

    def test_depends_on(self, tmp_path):
        defn = parse_dag_file(_write_dag(tmp_path))
        assert defn.tasks["task-b"].depends_on == ("task-a",)
        assert defn.tasks["task-a"].depends_on == ()

    def test_missing_dag_section(self, tmp_path):
        with pytest.raises(ValueError, match="Missing \\[dag\\] section"):
            parse_dag_file(_write_dag(tmp_path, '[tasks.x]\nsummary = "x"'))

    def test_missing_name(self, tmp_path):
        toml = '[dag]\nversion = 1\n[tasks.x]\nsummary = "x"'
        with pytest.raises(ValueError, match="Missing dag.name"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_missing_tasks_section(self, tmp_path):
        toml = '[dag]\nversion = 1\nname = "x"'
        with pytest.raises(ValueError, match="Missing \\[tasks\\] section"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_task_missing_required_field(self, tmp_path):
        toml = '[dag]\nversion = 1\nname = "x"\n[tasks.bad]\nsummary = "x"'
        with pytest.raises(ValueError, match="missing required field"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_task_empty_prompt(self, tmp_path):
        toml = """
[dag]
version = 1
name = "x"
[tasks.bad]
summary = "x"
prompt = "   "
commit_message = "x"
agent = "qwen-35b"
files.edit = ["a.py"]
"""
        with pytest.raises(ValueError, match="prompt must not be empty"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_task_no_files(self, tmp_path):
        toml = """
[dag]
version = 1
name = "x"
[tasks.bad]
summary = "x"
prompt = "do it"
commit_message = "x"
agent = "qwen-35b"
"""
        with pytest.raises(ValueError, match="must specify at least one file"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_glob_in_files_rejected(self, tmp_path):
        toml = """
[dag]
version = 1
name = "x"
[tasks.bad]
summary = "x"
prompt = "do it"
commit_message = "x"
agent = "qwen-35b"
files.edit = ["src/*.py"]
"""
        with pytest.raises(ValueError, match="Globs not allowed"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_absolute_path_rejected(self, tmp_path):
        toml = """
[dag]
version = 1
name = "x"
[tasks.bad]
summary = "x"
prompt = "do it"
commit_message = "x"
agent = "qwen-35b"
files.edit = ["/etc/passwd"]
"""
        with pytest.raises(ValueError, match="must be relative"):
            parse_dag_file(_write_dag(tmp_path, toml))

    def test_defaults(self, tmp_path):
        defn = parse_dag_file(_write_dag(tmp_path))
        assert defn.default_max_retries == 1
        assert defn.merge_resolve == "skip"
        assert defn.merge_squash is True
        task_a = defn.tasks["task-a"]
        assert task_a.permission_mode == "bypassPermissions"
        assert task_a.timeout_s == 900

    def test_review_agent_field(self, tmp_path):
        """review_agent field is parsed from task spec."""
        toml_content = """
[dag]
version = 1
name = "test-dag"
[tasks.task-a]
summary = "First task"
prompt = "Do task A"
commit_message = "Complete A"
agent = "qwen-9b"
review_agent = "qwen-35b"
files.edit = ["src/a.py"]
"""
        defn = parse_dag_file(_write_dag(tmp_path, toml_content))
        assert defn.tasks["task-a"].review_agent == "qwen-35b"

    def test_review_agent_default_empty(self, tmp_path):
        """review_agent defaults to empty string."""
        defn = parse_dag_file(_write_dag(tmp_path))
        assert defn.tasks["task-a"].review_agent == ""
