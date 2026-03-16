"""Unit tests for DAG file parser."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from dgov.dag import DagDefinition, parse_dag_file

pytestmark = pytest.mark.unit

MINIMAL_TOML = textwrap.dedent("""\
    [dag]
    version = 1
    name = "test-dag"

    [tasks.T0]
    summary = "Test task"
    agent = "hunter"
    prompt = "Do something"
    commit_message = "Do something"

    [tasks.T0.files]
    create = ["src/foo.py"]
""")


def _write_toml(content: str) -> str:
    """Write TOML to a temp file and return path."""
    p = Path(tempfile.mkdtemp()) / "test.toml"
    p.write_text(content)
    return str(p)


class TestParseDagFile:
    def test_minimal_valid(self):
        dag = parse_dag_file(_write_toml(MINIMAL_TOML))
        assert isinstance(dag, DagDefinition)
        assert dag.name == "test-dag"
        assert "T0" in dag.tasks
        t = dag.tasks["T0"]
        assert t.agent == "hunter"
        assert t.files.create == ("src/foo.py",)
        assert t.permission_mode == "acceptEdits"
        assert t.timeout_s == 900

    def test_missing_dag_section(self):
        with pytest.raises(ValueError, match="Missing.*dag.*section"):
            parse_dag_file(_write_toml('[tasks.T0]\nsummary = "x"\n'))

    def test_missing_version(self):
        toml = textwrap.dedent("""\
            [dag]
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "p"
            commit_message = "c"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        with pytest.raises(ValueError, match="version"):
            parse_dag_file(_write_toml(toml))

    def test_missing_name(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "p"
            commit_message = "c"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        with pytest.raises(ValueError, match="name"):
            parse_dag_file(_write_toml(toml))

    def test_missing_tasks(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
        """)
        with pytest.raises(ValueError, match="tasks"):
            parse_dag_file(_write_toml(toml))

    def test_missing_required_field(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        with pytest.raises(ValueError, match="agent"):
            parse_dag_file(_write_toml(toml))

    def test_empty_prompt(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "   "
            commit_message = "c"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        with pytest.raises(ValueError, match="prompt must not be empty"):
            parse_dag_file(_write_toml(toml))

    def test_no_files(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
        """)
        with pytest.raises(ValueError, match="at least one file"):
            parse_dag_file(_write_toml(toml))

    def test_glob_rejected(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["src/*.py"]
        """)
        with pytest.raises(ValueError, match="Globs not allowed"):
            parse_dag_file(_write_toml(toml))

    def test_absolute_path_rejected(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["/abs/path.py"]
        """)
        with pytest.raises(ValueError, match="relative"):
            parse_dag_file(_write_toml(toml))

    def test_defaults_applied(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            default_permission_mode = "bypassPermissions"
            default_timeout_s = 600
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.tasks["T0"].permission_mode == "bypassPermissions"
        assert dag.tasks["T0"].timeout_s == 600

    def test_task_overrides_defaults(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            default_timeout_s = 600
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
            timeout_s = 1200
            [tasks.T0.files]
            create = ["f.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.tasks["T0"].timeout_s == 1200

    def test_file_paths_sorted(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "h"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            edit = ["src/b.py", "src/a.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.tasks["T0"].files.edit == ("src/a.py", "src/b.py")

    def test_multi_task(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "multi"
            [tasks.T0]
            summary = "first"
            agent = "hunter"
            prompt = "do first"
            commit_message = "first"
            [tasks.T0.files]
            create = ["a.py"]
            [tasks.T1]
            summary = "second"
            agent = "hunter"
            depends_on = ["T0"]
            prompt = "do second"
            commit_message = "second"
            [tasks.T1.files]
            edit = ["a.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert len(dag.tasks) == 2
        assert dag.tasks["T1"].depends_on == ("T0",)

    def test_escalation_chain(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "x"
            [tasks.T0]
            summary = "x"
            agent = "hunter"
            escalation = ["gemini", "claude"]
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["f.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.tasks["T0"].escalation == ("gemini", "claude")
