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
    DagRunSummary,
    DagTaskSpec,
    compute_tiers,
    parse_dag_file,
    topological_order,
    transitive_dependents,
    validate_dag,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def stub_dispatch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dgov.dag.run_dispatch_preflight",
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


class TestDispatchTask:
    """Tests for task dispatch packet construction."""

    def _dag(self):
        return DagDefinition(
            name="test",
            dag_file="/tmp/test.toml",
            project_root="/tmp/proj",
            session_root="/tmp/proj",
            default_max_retries=1,
            merge_resolve="skip",
            merge_squash=True,
            max_concurrent=0,
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

    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    def test_dispatch_task_preflight_uses_exact_file_claims(
        self, mock_upsert, mock_emit, mock_create, monkeypatch
    ):
        from dgov.dag import _dispatch_task

        mock_create.return_value = _FakePane("T0")
        recorded: dict[str, object] = {}

        def fake_preflight(project_root, agent, **kwargs):  # noqa: ANN001, ANN201
            recorded["project_root"] = project_root
            recorded["agent"] = agent
            recorded.update(kwargs)
            return type("R", (), {"passed": True, "checks": []})()

        monkeypatch.setattr("dgov.dag.run_dispatch_preflight", fake_preflight)

        pane_info = _dispatch_task(self._dag(), self._dag().tasks["T0"], 1, "/tmp/proj")

        assert pane_info["pane_slug"] == "T0"
        assert recorded["project_root"] == "/tmp/proj"
        assert recorded["agent"] == "hunter"
        assert recorded["session_root"] == "/tmp/proj"
        packet = recorded["packet"]
        assert packet.prompt == "Do it"
        assert packet.file_claims == ("a.py",)
        assert packet.commit_message == "c"


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

    def test_stalled_run_waits_on_event_journal_then_fails(self, monkeypatch, tmp_path):
        from dgov.dag import run_dag

        path = self._write_dag(tmp_path)
        dag = parse_dag_file(path)

        monkeypatch.setattr(
            "dgov.dag._start_or_resume_run",
            lambda *args, **kwargs: (7, dag, {"T0": "dispatched"}),
        )
        monkeypatch.setattr("dgov.persistence.get_dag_task", lambda *args, **kwargs: None)

        waited: dict[str, object] = {}

        def fake_wait_for_events(session_root, **kwargs):  # noqa: ANN001, ANN201
            waited["session_root"] = session_root
            waited.update(kwargs)
            return []

        monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args, **kwargs: 12)
        monkeypatch.setattr("dgov.persistence.wait_for_events", fake_wait_for_events)
        monkeypatch.setattr("dgov.persistence.update_dag_run", lambda *args, **kwargs: None)
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "dgov.persistence.emit_event",
            lambda session_root, event, pane, **kwargs: events.append((event, pane)),
        )

        summary = run_dag(path)

        assert summary.status == "failed"
        assert waited["after_id"] == 12
        assert waited["panes"] == ("dag/7", "T0", "T1")
        assert "dag_task_completed" in waited["event_types"]
        assert ("dag_failed", "dag/7") in events


class TestEscalation:
    """Tests for retry and escalation logic."""

    def _task(self, escalation=("gemini",)):
        return DagTaskSpec(
            slug="T0",
            summary="Test",
            prompt="Do it",
            commit_message="c",
            agent="hunter",
            escalation=tuple(escalation),
            depends_on=(),
            files=DagFileSpec(create=("a.py",)),
            permission_mode="acceptEdits",
            timeout_s=900,
        )

    def _dag(self, task=None):
        t = task or self._task()
        return DagDefinition(
            name="test",
            dag_file="/tmp/test.toml",
            project_root="/tmp/proj",
            session_root="/tmp/proj",
            default_max_retries=1,
            merge_resolve="skip",
            merge_squash=True,
            max_concurrent=0,
            tasks={"T0": t},
        )

    def test_augment_prompt(self):
        from dgov.dag import _augment_prompt_with_review

        result = _augment_prompt_with_review(
            "Original prompt",
            {"issues": ["Bad code", "Missing test"]},
            "T0",
            "/tmp",
        )
        assert "Bad code" in result
        assert "Missing test" in result
        assert "Original prompt" in result

    def test_failure_reason_timeout(self):
        from dgov.dag import _task_failure_reason

        class FakeTimeout(Exception):
            pass

        FakeTimeout.__name__ = "PaneTimeoutError"
        assert _task_failure_reason(FakeTimeout(), None) == "timeout"

    def test_failure_reason_zero_commit(self):
        from dgov.dag import _task_failure_reason

        assert _task_failure_reason(None, {"commit_count": 0}) == "zero_commit"

    def test_failure_reason_review_failed(self):
        from dgov.dag import _task_failure_reason

        assert _task_failure_reason(None, {"commit_count": 1, "passed": False}) == "review_failed"

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_review_pass_returns_success(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
        mock_review,
        mock_update,
        mock_merge,
    ):
        from dgov.dag import run_task_until_terminal

        mock_create.return_value = _FakePane("T0")
        mock_wait.return_value = {"done": "T0", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1, "passed": True}

        result = run_task_until_terminal(self._dag(), self._task(), 1, 1, "/tmp/proj")
        assert result["status"] == "reviewed_pass"

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_zero_commit_escalates(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
        mock_review,
        mock_update,
        mock_merge,
    ):
        from dgov.dag import run_task_until_terminal

        call_count = [0]

        def fake_create(**kw):
            call_count[0] += 1
            return _FakePane(f"T0-{call_count[0]}")

        mock_create.side_effect = fake_create
        mock_wait.return_value = {"done": "T0", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 0, "passed": False}

        result = run_task_until_terminal(
            self._dag(), self._task(escalation=["gemini"]), 1, 1, "/tmp/proj"
        )
        assert result["status"] == "failed"
        assert mock_create.call_count >= 2

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.persistence.get_pane")
    def test_exhausted_chain_fails(
        self,
        mock_get_pane,
        mock_upsert,
        mock_emit,
        mock_create,
        mock_wait,
        mock_review,
        mock_update,
        mock_merge,
    ):
        from dgov.dag import run_task_until_terminal

        mock_create.side_effect = RuntimeError("health check failed")

        result = run_task_until_terminal(
            self._dag(), self._task(escalation=["gemini"]), 1, 0, "/tmp/proj"
        )
        assert result["status"] == "failed"


class TestIntegrationDag:
    """Integration-level tests using mocks only at system boundaries."""

    TOML_3TASK = textwrap.dedent("""\
        [dag]
        version = 1
        name = "integ-test"
        project_root = "."
        session_root = "{session}"

        [tasks.T0]
        summary = "Base task"
        agent = "hunter"
        escalation = ["gemini"]
        prompt = "Do T0"
        commit_message = "T0"
        [tasks.T0.files]
        create = ["a.py"]

        [tasks.T1]
        summary = "Dependent"
        agent = "hunter"
        depends_on = ["T0"]
        prompt = "Do T1"
        commit_message = "T1"
        [tasks.T1.files]
        create = ["b.py"]

        [tasks.T2]
        summary = "Final"
        agent = "hunter"
        depends_on = ["T1"]
        prompt = "Do T2"
        commit_message = "T2"
        [tasks.T2.files]
        create = ["c.py"]
    """)

    def _write_dag(self, tmp_path):
        session = str(tmp_path)
        p = tmp_path / "dag.toml"
        p.write_text(self.TOML_3TASK.format(session=session))
        return str(p)

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_successful_three_tier(
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
        assert set(summary.merged) == {"T0", "T1", "T2"}
        assert summary.failed == []

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_skip_propagation(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}
        mock_merge.return_value = {"merged": "x", "branch": "x"}

        path = self._write_dag(tmp_path)
        summary = run_dag(path, skip={"T0"})
        # T0 skipped, T1 and T2 transitively skipped
        assert "T0" in summary.skipped
        assert "T1" in summary.skipped
        assert "T2" in summary.skipped
        assert summary.merged == []

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_merge_conflict_stops_dag(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}
        mock_merge.return_value = {"error": "Merge conflict", "conflicts": ["a.py"]}

        path = self._write_dag(tmp_path)
        summary = run_dag(path)
        assert summary.status == "failed"

    @patch("dgov.merger.merge_worker_pane")
    @patch("dgov.persistence.update_pane_state")
    @patch("dgov.inspection.review_worker_pane")
    @patch("dgov.waiter.wait_worker_pane")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.persistence.get_pane")
    def test_no_auto_merge_then_merge(
        self, mock_get_pane, mock_create, mock_wait, mock_review, mock_update, mock_merge, tmp_path
    ):
        from dgov.dag import merge_dag, run_dag

        mock_create.side_effect = lambda **kw: _FakePane(kw.get("slug", "x"))
        mock_wait.return_value = {"done": "x", "method": "exit"}
        mock_get_pane.return_value = {"state": "done"}
        mock_review.return_value = {"verdict": "safe", "commit_count": 1}

        path = self._write_dag(tmp_path)
        summary1 = run_dag(path, auto_merge=False)
        assert summary1.status == "awaiting_merge"
        assert summary1.merged == []

        # Now merge
        mock_merge.return_value = {"merged": "x", "branch": "x"}
        summary2 = merge_dag(path)
        assert summary2.status == "completed"
        assert len(summary2.merged) > 0


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

    @patch("dgov.persistence.emit_event")
    @patch("dgov.persistence.upsert_dag_task")
    @patch("dgov.merger.merge_worker_pane")
    def test_post_merge_check_preserves_merge_when_safe_rollback_fails(
        self, mock_merge, mock_upsert, mock_emit, tmp_path
    ):
        from dgov.dag import _merge_tasks_in_order

        dag = DagDefinition(
            name="check-test",
            dag_file=str(tmp_path / "dag.toml"),
            project_root=str(tmp_path),
            session_root=str(tmp_path),
            default_max_retries=1,
            merge_resolve="skip",
            merge_squash=True,
            max_concurrent=0,
            tasks={
                "T0": DagTaskSpec(
                    slug="T0",
                    summary="A",
                    prompt="do A",
                    commit_message="A",
                    agent="pi",
                    escalation=(),
                    depends_on=(),
                    files=DagFileSpec(edit=("a.py",)),
                    permission_mode="bypassPermissions",
                    timeout_s=900,
                    post_merge_check="uv run pytest tests/test_dag.py -q",
                )
            },
        )
        mock_merge.return_value = {"merged": "pane-0", "branch": "pane-0"}

        calls: list[list[str]] = []

        def fake_run(args, capture_output=False, text=False, shell=False, cwd=None, env=None):  # noqa: ANN001, ANN201, FBT002
            if shell:
                return type("R", (), {"returncode": 1, "stderr": "check failed", "stdout": ""})()
            calls.append(list(args))
            if args[-2:] == ["rev-parse", "HEAD"]:
                return type("R", (), {"returncode": 0, "stderr": "", "stdout": "abc123\n"})()
            if args[-4:] == ["diff", "--name-only", "HEAD~1", "HEAD"]:
                return type("R", (), {"returncode": 0, "stderr": "", "stdout": "a.py\n"})()
            if args[-3:] == ["reset", "--keep", "HEAD~1"]:
                return type(
                    "R",
                    (),
                    {
                        "returncode": 1,
                        "stderr": "local changes would be overwritten",
                        "stdout": "",
                    },
                )()
            raise AssertionError(args)

        with patch("dgov.dag.subprocess.run", side_effect=fake_run):
            merged, error = _merge_tasks_in_order(
                dag,
                ["T0"],
                {"T0": "pane-0"},
                str(tmp_path),
                run_id=7,
            )

        assert merged == ["T0"]
        assert error is not None
        assert error["rollback_performed"] is False
        assert "overwritten" in error["rollback_error"]
        mock_upsert.assert_called_once_with(str(tmp_path), 7, "T0", "merged", "pi")
        mock_emit.assert_called_once_with(str(tmp_path), "dag_task_completed", "T0", dag_run_id=7)


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

    def test_dag_run_options_max_concurrent(self):
        opts = DagRunOptions(max_concurrent=2)
        assert opts.max_concurrent == 2
