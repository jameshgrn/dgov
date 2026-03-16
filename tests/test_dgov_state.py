"""Unit tests for dgov status command (formerly dgov.state)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


class TestStatusCommand:
    def test_returns_expected_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.status.list_worker_panes",
            lambda *a, **kw: [{"alive": True}, {"alive": False}],
        )
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 2
        assert data["alive"] == 1

    def test_empty_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["alive"] == 0


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
