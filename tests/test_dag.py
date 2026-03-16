"""Unit tests for DAG file parser."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.dag import (
    DagDefinition,
    DagFileSpec,
    DagRunOptions,
    DagTaskSpec,
    compute_tiers,
    parse_dag_file,
    topological_order,
    transitive_dependents,
    validate_dag,
)

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


def _task(slug, depends_on=(), files_edit=()):
    """Helper to create a minimal DagTaskSpec for testing."""
    return DagTaskSpec(
        slug=slug,
        summary=f"Task {slug}",
        prompt=f"Do {slug}",
        commit_message=f"Commit {slug}",
        agent="hunter",
        escalation=(),
        depends_on=tuple(depends_on),
        files=DagFileSpec(edit=tuple(sorted(files_edit))),
        permission_mode="acceptEdits",
        timeout_s=900,
    )


class TestValidateDag:
    def test_valid_dag(self):
        tasks = {"T0": _task("T0"), "T1": _task("T1", depends_on=["T0"])}
        validate_dag(tasks)  # should not raise

    def test_missing_dep(self):
        tasks = {"T0": _task("T0", depends_on=["T_MISSING"])}
        with pytest.raises(ValueError, match="does not exist"):
            validate_dag(tasks)

    def test_self_cycle(self):
        tasks = {"T0": _task("T0", depends_on=["T0"])}
        with pytest.raises(ValueError, match="cycle"):
            validate_dag(tasks)

    def test_multi_node_cycle(self):
        tasks = {
            "A": _task("A", depends_on=["C"]),
            "B": _task("B", depends_on=["A"]),
            "C": _task("C", depends_on=["B"]),
        }
        with pytest.raises(ValueError, match="cycle"):
            validate_dag(tasks)


class TestTopologicalOrder:
    def test_linear(self):
        tasks = {
            "T0": _task("T0"),
            "T1": _task("T1", depends_on=["T0"]),
            "T2": _task("T2", depends_on=["T1"]),
        }
        order = topological_order(tasks)
        assert order.index("T0") < order.index("T1") < order.index("T2")

    def test_diamond(self):
        tasks = {
            "T0": _task("T0"),
            "T1": _task("T1", depends_on=["T0"]),
            "T2": _task("T2", depends_on=["T0"]),
            "T3": _task("T3", depends_on=["T1", "T2"]),
        }
        order = topological_order(tasks)
        assert order.index("T0") < order.index("T1")
        assert order.index("T0") < order.index("T2")
        assert order.index("T1") < order.index("T3")
        assert order.index("T2") < order.index("T3")

    def test_stable_order(self):
        tasks = {"B": _task("B"), "A": _task("A"), "C": _task("C")}
        order = topological_order(tasks)
        assert order == ["A", "B", "C"]


class TestComputeTiers:
    def test_single_tier(self):
        tasks = {"T0": _task("T0", files_edit=["a.py"]), "T1": _task("T1", files_edit=["b.py"])}
        tiers = compute_tiers(tasks)
        assert len(tiers) == 1
        assert set(tiers[0]) == {"T0", "T1"}

    def test_overlap_serializes(self):
        tasks = {
            "T0": _task("T0", files_edit=["src/dag.py"]),
            "T1": _task("T1", files_edit=["src/dag.py"]),
        }
        tiers = compute_tiers(tasks)
        assert len(tiers) == 2

    def test_ancestor_overlap(self):
        tasks = {
            "T0": _task("T0", files_edit=["src/dgov/"]),
            "T1": _task("T1", files_edit=["src/dgov/dag.py"]),
        }
        tiers = compute_tiers(tasks)
        assert len(tiers) == 2

    def test_dependency_respects_tier(self):
        tasks = {
            "T0": _task("T0", files_edit=["a.py"]),
            "T1": _task("T1", depends_on=["T0"], files_edit=["b.py"]),
        }
        tiers = compute_tiers(tasks)
        assert len(tiers) == 2
        assert tiers[0] == ["T0"]
        assert tiers[1] == ["T1"]


class TestTransitiveDependents:
    def test_direct_dependent(self):
        tasks = {"T0": _task("T0"), "T1": _task("T1", depends_on=["T0"])}
        deps = transitive_dependents(tasks, {"T0"})
        assert deps == {"T1"}

    def test_transitive(self):
        tasks = {
            "T0": _task("T0"),
            "T1": _task("T1", depends_on=["T0"]),
            "T2": _task("T2", depends_on=["T1"]),
        }
        deps = transitive_dependents(tasks, {"T0"})
        assert deps == {"T1", "T2"}

    def test_no_dependents(self):
        tasks = {"T0": _task("T0"), "T1": _task("T1")}
        deps = transitive_dependents(tasks, {"T0"})
        assert deps == set()


class TestDashboardFixture:
    """Verify the dashboard DAG fixture parses and validates."""

    FIXTURE = str(Path(__file__).parent / "fixtures" / "dashboard_dag.toml")

    def test_parse_fixture(self):
        dag = parse_dag_file(self.FIXTURE)
        assert dag.name == "dashboard-v2"
        assert len(dag.tasks) == 7

    def test_expected_tasks(self):
        dag = parse_dag_file(self.FIXTURE)
        expected = {"T0a", "T0b", "T0c", "T1a", "T2a", "T3a", "T4a"}
        assert set(dag.tasks.keys()) == expected

    def test_dependencies(self):
        dag = parse_dag_file(self.FIXTURE)
        assert dag.tasks["T0b"].depends_on == ("T0a",)
        assert dag.tasks["T2a"].depends_on == ("T0b", "T1a")
        assert dag.tasks["T4a"].depends_on == ("T1a", "T2a", "T3a")

    def test_agents(self):
        dag = parse_dag_file(self.FIXTURE)
        for task in dag.tasks.values():
            assert task.agent == "hunter"
            assert task.escalation == ("gemini",)


class _FakePane:
    """Minimal fake for create_worker_pane return."""

    def __init__(self, slug):
        self.slug = slug


class TestRunSingleTier:
    """Tests for single-tier execution."""

    def _dag(self):
        return DagDefinition(
            name="test",
            dag_file="/tmp/test.toml",
            project_root="/tmp/proj",
            session_root="/tmp/proj",
            default_max_retries=1,
            merge_resolve="skip",
            merge_squash=True,
            tasks={
                "T0": DagTaskSpec(
                    slug="T0",
                    summary="Test",
                    prompt="Do it",
                    commit_message="c",
                    agent="hunter",
                    escalation=(),
                    depends_on=(),
                    files=DagFileSpec(create=("a.py",)),
                    permission_mode="acceptEdits",
                    timeout_s=900,
                ),
            },
        )

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_single_task_success(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
        mock_review,
        mock_update_state,
        mock_merge,
    ):
        from dgov.dag import run_single_tier

        mock_create.return_value = _FakePane("T0")
        mock_wait.return_value = {"done": "T0", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}
        mock_merge.return_value = {"merged": "T0", "branch": "T0"}

        dag = self._dag()
        opts = DagRunOptions(auto_merge=True)
        states: dict[str, str] = {}
        result = run_single_tier(dag, ["T0"], 1, states, opts, "/tmp/proj")

        assert "T0" in result["reviewed_pass"]
        assert "T0" in result["merged"]
        assert result["merge_error"] is None

    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_merge_error_stops(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
        mock_review,
        mock_update_state,
    ):
        from dgov.dag import run_single_tier

        mock_create.return_value = _FakePane("T0")
        mock_wait.return_value = {"done": "T0", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}

        dag = self._dag()
        opts = DagRunOptions(auto_merge=True)
        states: dict[str, str] = {}

        with patch("dgov.merger.merge_worker_pane") as mock_merge:
            mock_merge.return_value = {"error": "Merge conflict", "conflicts": ["a.py"]}
            result = run_single_tier(dag, ["T0"], 1, states, opts, "/tmp/proj")

        assert result["merge_error"] is not None

    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_failed_pane_skips_review(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
    ):
        from dgov.dag import run_single_tier

        mock_create.return_value = _FakePane("T0")
        mock_wait.return_value = {"done": "T0", "method": "exit"}
        mock_get_pane.return_value = {"state": "failed"}

        dag = self._dag()
        opts = DagRunOptions(auto_merge=True)
        states: dict[str, str] = {}
        result = run_single_tier(dag, ["T0"], 1, states, opts, "/tmp/proj")

        assert "T0" in result["failed"]
        assert result["reviewed_pass"] == []


class TestRunDag:
    """Tests for multi-tier orchestration."""

    TOML_2TIER = textwrap.dedent("""\
        [dag]
        version = 1
        name = "two-tier"
        project_root = "."
        session_root = "{session}"

        [tasks.T0]
        summary = "First"
        agent = "hunter"
        prompt = "Do T0"
        commit_message = "T0"
        [tasks.T0.files]
        create = ["a.py"]

        [tasks.T1]
        summary = "Second"
        agent = "hunter"
        depends_on = ["T0"]
        prompt = "Do T1"
        commit_message = "T1"
        [tasks.T1.files]
        create = ["b.py"]
    """)

    def _write_dag(self, tmp_path, session_root=None):
        session = session_root or str(tmp_path)
        p = tmp_path / "dag.toml"
        p.write_text(self.TOML_2TIER.format(session=session))
        return str(p)

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_dry_run(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        path = self._write_dag(tmp_path)
        summary = run_dag(path, dry_run=True)
        assert summary.status == "dry_run"
        assert summary.run_id == 0
        # No panes should be created
        mock_create.assert_not_called()

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_two_tier_success(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}
        mock_merge.return_value = {"merged": "x", "branch": "x"}

        path = self._write_dag(tmp_path)
        summary = run_dag(path)
        assert summary.status == "completed"
        assert "T0" in summary.merged
        assert "T1" in summary.merged

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_tier_limit(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}
        mock_merge.return_value = {"merged": "x", "branch": "x"}

        path = self._write_dag(tmp_path)
        summary = run_dag(path, tier_limit=0)
        # Only T0 should have been dispatched (tier 0)
        assert "T0" in summary.merged
        assert "T1" not in summary.merged

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_no_auto_merge(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}

        path = self._write_dag(tmp_path)
        summary = run_dag(path, auto_merge=False)
        assert summary.status == "awaiting_merge"
        assert summary.merged == []
        mock_merge.assert_not_called()
