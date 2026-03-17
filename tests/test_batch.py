from __future__ import annotations

import json
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

        # Mock DAG helpers
        monkeypatch.setattr(
            "dgov.batch._compute_tiers",
            lambda tasks: [[tasks["t1"]]],
        )
        monkeypatch.setattr(
            "dgov.batch._transitive_dependents",
            lambda tasks, failed_ids: set(),
        )

        # Mock create_worker_pane and merge_worker_pane (from lifecycle/merger)
        class Pane:
            def __init__(self, slug: str) -> None:
                self.slug = slug

        created = []

        def fake_create_worker_pane(**kwargs):  # noqa: ANN003, ANN201
            created.append(kwargs)
            return Pane(slug=kwargs["slug"])

        def fake_merge_worker_pane(project_root_arg, slug, session_root=None):  # noqa: ANN001, ANN002, ANN003
            return {"merged": True, "slug": slug}

        monkeypatch.setattr("dgov.lifecycle.create_worker_pane", fake_create_worker_pane)
        monkeypatch.setattr("dgov.merger.merge_worker_pane", fake_merge_worker_pane)

        # Avoid real waits
        monkeypatch.setattr("dgov.batch.wait_for_slugs", lambda sr, slugs, timeout=600: [])

        result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == ["t1"]
        assert created == [
            {
                "project_root": ".",
                "prompt": "do a thing",
                "agent": "agent-one",
                "permission_mode": "bypassPermissions",
                "slug": "t1",
                "session_root": str(session_root),
            }
        ]

    def test_batch_dispatch_invalid_toml_raises(self, tmp_path):
        spec = tmp_path / "bad.toml"
        spec.write_text("this is not valid toml =")  # invalid

        with pytest.raises(Exception):
            batch_dispatch(str(spec), session_root=str(tmp_path))
