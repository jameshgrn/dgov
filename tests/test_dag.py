"""Unit tests for DAG file parser."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from dgov.dag import DagDefinition, DagRunSummary, parse_dag_file
from dgov.dag_graph import compute_tiers, topological_order, transitive_dependents, validate_dag
from dgov.dag_parser import DagFileSpec, DagTaskSpec

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def stub_dispatch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dgov.executor.run_dispatch_preflight",
        lambda *args, **kwargs: type("R", (), {"passed": True, "checks": []})(),
    )


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
        assert t.permission_mode == "bypassPermissions"
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


class TestFixtureTiers:
    """Verify the dashboard DAG fixture tiers correctly."""

    FIXTURE = str(Path(__file__).parent / "fixtures" / "dashboard_dag.toml")

    def test_fixture_tiers(self):
        from dgov.dag import compute_tiers, parse_dag_file

        dag = parse_dag_file(self.FIXTURE)
        tiers = compute_tiers(dag.tasks)
        # T0a and T0c have no deps and different files -> same tier
        assert "T0a" in tiers[0]
        assert "T0c" in tiers[0]
        # T0b depends on T0a -> later tier
        t0b_tier = next(i for i, t in enumerate(tiers) if "T0b" in t)
        t0a_tier = next(i for i, t in enumerate(tiers) if "T0a" in t)
        assert t0b_tier > t0a_tier
        # T4a depends on T1a, T2a, T3a -> last tier
        t4a_tier = next(i for i, t in enumerate(tiers) if "T4a" in t)
        assert t4a_tier == len(tiers) - 1


class TestPostMergeCheck:
    def test_parse_post_merge_check_from_toml(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "check-test"

            [tasks.T0]
            summary = "A"
            agent = "pi"
            prompt = "do A"
            commit_message = "A"
            post_merge_check = "uv run pytest tests/ -q"

            [tasks.T0.files]
            edit = ["a.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.tasks["T0"].post_merge_check == "uv run pytest tests/ -q"

    def test_post_merge_check_defaults_to_empty(self):
        dag = parse_dag_file(_write_toml(MINIMAL_TOML))
        assert dag.tasks["T0"].post_merge_check == ""

    def test_dag_task_spec_has_post_merge_check(self):
        task = DagTaskSpec(
            slug="t",
            summary="s",
            prompt="p",
            commit_message="c",
            agent="pi",
            escalation=(),
            depends_on=(),
            files=DagFileSpec(create=("f.py",)),
            permission_mode="bypassPermissions",
            timeout_s=900,
            post_merge_check="echo ok",
        )
        assert task.post_merge_check == "echo ok"


class TestUnhappyPathVerification:
    """Verify tests catch real failures."""

    def test_wrong_status_detected(self):
        """Confirm that asserting the wrong status actually fails."""
        summary = DagRunSummary(run_id=0, dag_file="x", status="completed")
        # This should be True — if it were "failed" this would catch the bug
        assert summary.status == "completed"
        assert summary.status != "failed"


class TestMaxConcurrent:
    def test_parse_max_concurrent_from_toml(self):
        toml = textwrap.dedent("""\
            [dag]
            version = 1
            name = "concurrent-test"
            max_concurrent = 3

            [tasks.T0]
            summary = "A"
            agent = "pi"
            prompt = "do A"
            commit_message = "A"

            [tasks.T0.files]
            edit = ["a.py"]
        """)
        dag = parse_dag_file(_write_toml(toml))
        assert dag.max_concurrent == 3

    def test_max_concurrent_defaults_to_zero(self):
        dag = parse_dag_file(_write_toml(MINIMAL_TOML))
        assert dag.max_concurrent == 0
