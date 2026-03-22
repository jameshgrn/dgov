"""Unit tests for dgov status command (formerly dgov.state)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


class TestStatusCommand:
    def test_returns_expected_keys_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.status.list_worker_panes",
            lambda *a, **kw: [{"alive": True}, {"alive": False}],
        )
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 2
        assert data["alive"] == 1

    def test_empty_panes_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["alive"] == 0

    def test_human_readable_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "dgov status:" in result.output


class TestDagPersistence:
    """Tests for DAG run and task persistence."""

    def _make_session(self, tmp_path):
        """Create a temp session root with initialized DB."""
        from dgov.persistence import ensure_dag_tables

        session = str(tmp_path / "test-session")
        Path(session).mkdir(parents=True, exist_ok=True)
        ensure_dag_tables(session)
        return session

    def test_ensure_dag_tables_idempotent(self, tmp_path):
        from dgov.persistence import ensure_dag_tables

        session = str(tmp_path / "test-session")
        Path(session).mkdir(parents=True, exist_ok=True)
        ensure_dag_tables(session)
        ensure_dag_tables(session)  # second call should not fail

    def test_create_and_get_dag_run(self, tmp_path):
        from dgov.persistence import create_dag_run, get_dag_run

        session = self._make_session(tmp_path)
        run_id = create_dag_run(
            session, "/path/to/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {"tiers": [["T0"]]}
        )
        assert isinstance(run_id, int)
        run = get_dag_run(session, run_id)
        assert run is not None
        assert run["dag_file"] == "/path/to/dag.toml"
        assert run["status"] == "running"
        assert run["state_json"]["tiers"] == [["T0"]]

    def test_get_open_dag_run(self, tmp_path):
        from dgov.persistence import create_dag_run, get_open_dag_run

        session = self._make_session(tmp_path)
        create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {})
        run = get_open_dag_run(session, "/dag.toml")
        assert run is not None
        assert run["status"] == "running"

    def test_get_open_dag_run_ignores_completed(self, tmp_path):
        from dgov.persistence import create_dag_run, get_open_dag_run

        session = self._make_session(tmp_path)
        create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "completed", 0, {})
        run = get_open_dag_run(session, "/dag.toml")
        assert run is None

    def test_update_dag_run(self, tmp_path):
        from dgov.persistence import create_dag_run, get_dag_run, update_dag_run

        session = self._make_session(tmp_path)
        run_id = create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {})
        update_dag_run(
            session, run_id, status="completed", current_tier=2, state_json={"done": True}
        )
        run = get_dag_run(session, run_id)
        assert run["status"] == "completed"
        assert run["current_tier"] == 2
        assert run["state_json"]["done"] is True

    def test_upsert_dag_task_insert(self, tmp_path):
        from dgov.persistence import create_dag_run, list_dag_tasks, upsert_dag_task

        session = self._make_session(tmp_path)
        run_id = create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {})
        upsert_dag_task(session, run_id, "T0", "pending", "hunter")
        tasks = list_dag_tasks(session, run_id)
        assert len(tasks) == 1
        assert tasks[0]["slug"] == "T0"
        assert tasks[0]["status"] == "pending"

    def test_upsert_dag_task_update(self, tmp_path):
        from dgov.persistence import create_dag_run, list_dag_tasks, upsert_dag_task

        session = self._make_session(tmp_path)
        run_id = create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {})
        upsert_dag_task(session, run_id, "T0", "pending", "hunter")
        upsert_dag_task(
            session, run_id, "T0", "dispatched", "hunter", attempt=1, pane_slug="T0-abc"
        )
        tasks = list_dag_tasks(session, run_id)
        assert len(tasks) == 1
        assert tasks[0]["status"] == "dispatched"
        assert tasks[0]["pane_slug"] == "T0-abc"

    def test_list_dag_tasks_ordered_by_slug(self, tmp_path):
        from dgov.persistence import create_dag_run, list_dag_tasks, upsert_dag_task

        session = self._make_session(tmp_path)
        run_id = create_dag_run(session, "/dag.toml", "2024-01-01T00:00:00Z", "running", 0, {})
        upsert_dag_task(session, run_id, "T2", "pending", "hunter")
        upsert_dag_task(session, run_id, "T0", "pending", "hunter")
        upsert_dag_task(session, run_id, "T1", "pending", "hunter")
        tasks = list_dag_tasks(session, run_id)
        slugs = [t["slug"] for t in tasks]
        assert slugs == ["T0", "T1", "T2"]

    def test_resume_lookup_by_absolute_path(self, tmp_path):
        from dgov.persistence import create_dag_run, get_open_dag_run

        session = self._make_session(tmp_path)
        create_dag_run(
            session,
            "/abs/path/dag.toml",
            "2024-01-01T00:00:00Z",
            "running",
            0,
            {"dag_sha256": "abc123"},
        )
        # Different relative path should not match
        run = get_open_dag_run(session, "dag.toml")
        assert run is None
        # Exact absolute path matches
        run = get_open_dag_run(session, "/abs/path/dag.toml")
        assert run is not None


class TestDagEvents:
    """Tests for DAG lifecycle events."""

    def _make_session(self, tmp_path):
        from dgov.persistence import ensure_dag_tables

        session = str(tmp_path / "test-session")
        Path(session).mkdir(parents=True, exist_ok=True)
        ensure_dag_tables(session)
        return session

    @pytest.mark.parametrize(
        "event_name",
        [
            "dag_started",
            "dag_tier_started",
            "dag_task_dispatched",
            "dag_task_completed",
            "dag_task_failed",
            "dag_task_escalated",
            "dag_tier_completed",
            "dag_completed",
            "dag_failed",
        ],
    )
    def test_emit_dag_event_accepted(self, tmp_path, event_name):
        from dgov.persistence import emit_event, read_events

        session = self._make_session(tmp_path)
        emit_event(session, event_name, "dag/1", dag_run_id=1, tier=0)
        events = read_events(session)
        assert any(e["event"] == event_name for e in events)

    def test_dag_event_payload_intact(self, tmp_path):
        from dgov.persistence import emit_event, read_events

        session = self._make_session(tmp_path)
        emit_event(
            session, "dag_task_dispatched", "T0", dag_run_id=42, tier=1, agent="hunter", attempt=1
        )
        events = read_events(session)
        ev = [e for e in events if e["event"] == "dag_task_dispatched"][0]
        assert ev["dag_run_id"] == 42
        assert ev["agent"] == "hunter"

    def test_run_level_event_uses_dag_pane_prefix(self, tmp_path):
        from dgov.persistence import emit_event, read_events

        session = self._make_session(tmp_path)
        emit_event(session, "dag_started", "dag/12", dag_run_id=12)
        events = read_events(session)
        ev = [e for e in events if e["event"] == "dag_started"][0]
        assert ev["pane"] == "dag/12"

    def test_task_level_event_uses_task_slug(self, tmp_path):
        from dgov.persistence import emit_event, read_events

        session = self._make_session(tmp_path)
        emit_event(session, "dag_task_completed", "T3", dag_run_id=12, tier=2)
        events = read_events(session)
        ev = [e for e in events if e["event"] == "dag_task_completed"][0]
        assert ev["pane"] == "T3"

    def test_latest_event_id_and_wait_for_events(self, tmp_path, monkeypatch):
        from dgov.persistence import emit_event, latest_event_id, wait_for_events

        session = self._make_session(tmp_path)
        emit_event(session, "dag_started", "dag/1", dag_run_id=1)
        cursor = latest_event_id(session)

        def fake_sleep(seconds: float) -> None:
            emit_event(session, "dag_task_completed", "T0", dag_run_id=1, tier=0)

        monkeypatch.setattr("dgov.persistence.time.sleep", fake_sleep)
        events = wait_for_events(
            session,
            after_id=cursor,
            panes=("T0",),
            event_types=("dag_task_completed",),
            timeout_s=0.5,
        )

        assert len(events) == 1
        assert events[0]["id"] > cursor
        assert events[0]["event"] == "dag_task_completed"
        assert events[0]["pane"] == "T0"

    def test_wait_for_events_returns_empty_on_timeout(self, tmp_path):
        from dgov.persistence import latest_event_id, wait_for_events

        session = self._make_session(tmp_path)
        events = wait_for_events(
            session,
            after_id=latest_event_id(session),
            panes=("T0",),
            event_types=("dag_task_completed",),
            timeout_s=0.01,
            poll_interval_s=0.01,
        )

        assert events == []
