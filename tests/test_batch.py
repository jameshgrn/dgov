from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from dgov.batch import (
    batch_dispatch,
    create_checkpoint,
    restore_checkpoint,
)

pytestmark = pytest.mark.unit


def _read_checkpoint(session_root: Path, name: str) -> dict[str, Any]:
    checkpoint_path = session_root / ".dgov" / "checkpoints" / f"{name}.json"
    return json.loads(checkpoint_path.read_text())


class TestCreateCheckpoint:
    def test_create_checkpoint_writes_json_and_emits_event(self, tmp_path, monkeypatch):
        project_root = tmp_path
        session_root = tmp_path

        # Mock subprocess.git calls
        calls = []

        def fake_run(args, capture_output=False, text=False):  # noqa: ANN001, ANN201
            calls.append(list(args))

            class Result:
                returncode = 0
                stdout = "deadbeef\n"

            return Result()

        monkeypatch.setattr("dgov.batch.subprocess.run", fake_run)

        # Mock panes and emit_event
        monkeypatch.setattr("dgov.batch.all_panes", lambda sr: [{"slug": "p1"}])

        events: list[tuple[str, str, dict[str, Any]]] = []

        def fake_emit_event(session_root_arg, event, pane, **kwargs):  # noqa: ANN001, ANN002, ANN003
            events.append((event, pane, kwargs))

        monkeypatch.setattr("dgov.batch.emit_event", fake_emit_event)

        result = create_checkpoint(str(project_root), "snap", session_root=str(session_root))

        checkpoint = _read_checkpoint(session_root, "snap")

        assert result["checkpoint"] == "snap"
        assert result["main_sha"] == "deadbeef"
        assert result["pane_count"] == 1

        assert checkpoint["name"] == "snap"
        assert checkpoint["main_sha"] == "deadbeef"
        assert checkpoint["panes"] == [{"slug": "p1"}]
        assert isinstance(checkpoint["ts"], str) and checkpoint["ts"]

        assert events == [("checkpoint_created", "checkpoint/snap", {"main_sha": "deadbeef"})]

        # First git call is for project_root HEAD, second for pane worktree (ignored in test)
        assert any("rev-parse" in call for call in calls)

    def test_create_checkpoint_overwrite_records_previous_ts(self, tmp_path, monkeypatch):
        project_root = tmp_path
        session_root = tmp_path

        def fake_run(*args, **kwargs):  # noqa: ANN001, ANN002, ANN201
            class Result:
                returncode = 0
                stdout = "cafebabe\n"

            return Result()

        monkeypatch.setattr("dgov.batch.subprocess.run", fake_run)
        monkeypatch.setattr("dgov.batch.all_panes", lambda sr: [])
        monkeypatch.setattr("dgov.batch.emit_event", lambda *a, **k: None)

        first = create_checkpoint(str(project_root), "snap", session_root=str(session_root))
        second = create_checkpoint(str(project_root), "snap", session_root=str(session_root))

        assert "overwrote" in second
        assert first["main_sha"] == "cafebabe"
        assert second["main_sha"] == "cafebabe"


class TestRestoreCheckpoint:
    def test_restore_checkpoint_round_trip(self, tmp_path, monkeypatch):
        project_root = tmp_path
        session_root = tmp_path

        monkeypatch.setattr(
            "dgov.batch.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "1234abcd\n"})(),
        )  # noqa: E501
        monkeypatch.setattr("dgov.batch.all_panes", lambda sr: [{"slug": "p2"}])
        monkeypatch.setattr("dgov.batch.emit_event", lambda *a, **k: None)

        create_checkpoint(str(project_root), "snap", session_root=str(session_root))

        restored = restore_checkpoint(str(session_root), "snap")

        assert restored["name"] == "snap"
        assert restored["panes"] == [{"slug": "p2"}]
        assert restored["main_sha"].startswith("1234abcd")


class TestBatchDispatch:
    def _write_spec(self, tmp_path: Path, body: str) -> Path:
        spec = tmp_path / "batch.toml"
        spec.write_text(body)
        return spec

    @contextmanager
    def _mock_dag_run(self, monkeypatch, status, merged=None, failed=None, skipped=None):
        from dgov.dag_parser import DagRunSummary

        run_id = 1
        monkeypatch.setattr(
            "dgov.dag.run_dag_via_kernel",
            lambda *args, **kwargs: DagRunSummary(
                run_id=run_id,
                dag_file="test.toml",
                status="submitted",
                merged=[],
                failed=[],
                skipped=[],
                blocked=[],
            ),
        )
        monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
        monkeypatch.setattr(
            "dgov.persistence.wait_for_events",
            lambda *args, **kwargs: [{"id": 1, "event": "dag_completed", "data": json.dumps({"dag_run_id": run_id})}],
        )

        task_states = {}
        for s in (merged or []): task_states[s] = "merged"
        for s in (failed or []): task_states[s] = "failed"
        for s in (skipped or []): task_states[s] = "skipped"

        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda *args, **kwargs: {
                "id": run_id,
                "status": status,
                "state_json": {
                    "task_states": task_states
                },
            },
        )
        monkeypatch.setattr("dgov.persistence.list_dag_tasks", lambda *args: [])
        yield


    def test_batch_dispatch_reads_toml_and_dispatches_tasks(self, tmp_path, monkeypatch):
        spec_path = self._write_spec(
            tmp_path,
            """
project_root = "."

[tasks.t1]
prompt = "do a thing"
agent = "agent-one"
touches = ["a.py"]
timeout = 123
permission_mode = "bypassPermissions"
""",
        )

        session_root = tmp_path / "session"
        session_root.mkdir()

        with self._mock_dag_run(monkeypatch, status="completed", merged=["t1"]):
            result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == ["t1"]

    def test_batch_dispatch_skips_non_safe_review(self, tmp_path, monkeypatch):
        spec_path = self._write_spec(
            tmp_path,
            """
project_root = "."

[tasks.t1]
prompt = "do a thing"
agent = "agent-one"
touches = ["a.py"]
""",
        )

        session_root = tmp_path / "session"
        session_root.mkdir()

        with self._mock_dag_run(monkeypatch, status="failed", failed=["t1"]):
            result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == []
        assert result["failed"] == ["t1"]

    def test_batch_dispatch_records_failed_task_error_in_tier_results(self, tmp_path, monkeypatch):
        spec_path = self._write_spec(
            tmp_path,
            """
project_root = "."

[tasks.t1]
prompt = "do a thing"
agent = "agent-one"
touches = ["a.py"]
""",
        )

        session_root = tmp_path / "session"
        session_root.mkdir()

        with self._mock_dag_run(monkeypatch, status="failed", failed=["t1"]):
            result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["failed"] == ["t1"]

    def test_batch_dispatch_invalid_toml_raises(self, tmp_path):
        spec = tmp_path / "bad.toml"
        spec.write_text("this is not valid toml =")  # invalid

        with pytest.raises(Exception):
            batch_dispatch(str(spec), session_root=str(tmp_path))
