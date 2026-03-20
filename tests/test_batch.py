from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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
        monkeypatch.setattr(
            "dgov.preflight.run_preflight",
            lambda *args, **kwargs: type("R", (), {"passed": True})(),
        )

        # Mock create_worker_pane and canonical post-dispatch lifecycle.
        class Pane:
            def __init__(self, slug: str) -> None:
                self.slug = slug

        created = []

        def fake_create_worker_pane(**kwargs):  # noqa: ANN003, ANN201
            created.append(kwargs)
            return Pane(slug=kwargs["slug"])

        monkeypatch.setattr("dgov.lifecycle.create_worker_pane", fake_create_worker_pane)
        monkeypatch.setattr(
            "dgov.executor.run_post_dispatch_lifecycle",
            lambda project_root, slug, **kwargs: SimpleNamespace(
                state="completed",
                slug=slug,
                merge_result={"merged": True, "slug": slug},
                failure_stage=None,
            ),
        )

        result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == ["t1"]
        assert len(created) == 1
        assert created[0]["project_root"] == "."
        assert created[0]["prompt"] == "do a thing"
        assert created[0]["agent"] == "agent-one"
        assert created[0]["permission_mode"] == "bypassPermissions"
        assert created[0]["slug"] == "t1"
        assert created[0]["session_root"] == str(session_root)
        assert created[0]["context_packet"].file_claims == ("a.py",)

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

        monkeypatch.setattr("dgov.batch._compute_tiers", lambda tasks: [[tasks["t1"]]])
        monkeypatch.setattr("dgov.batch._transitive_dependents", lambda tasks, failed_ids: set())

        class Pane:
            def __init__(self, slug: str) -> None:
                self.slug = slug

        monkeypatch.setattr(
            "dgov.lifecycle.create_worker_pane",
            lambda **kwargs: Pane(slug=kwargs["slug"]),
        )
        monkeypatch.setattr(
            "dgov.preflight.run_preflight",
            lambda *args, **kwargs: type("R", (), {"passed": True})(),
        )
        monkeypatch.setattr(
            "dgov.executor.run_post_dispatch_lifecycle",
            lambda project_root, slug, **kwargs: SimpleNamespace(
                state="review_pending",
                slug=slug,
                merge_result=None,
                failure_stage=None,
            ),
        )

        result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == []
        assert result["failed"] == ["t1"]

    def test_batch_dispatch_blocks_preflight_failure(self, tmp_path, monkeypatch):
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

        monkeypatch.setattr("dgov.batch._compute_tiers", lambda tasks: [[tasks["t1"]]])
        monkeypatch.setattr("dgov.batch._transitive_dependents", lambda tasks, failed_ids: set())
        monkeypatch.setattr(
            "dgov.preflight.run_preflight",
            lambda *args, **kwargs: type("R", (), {"passed": False})(),
        )
        create_calls: list[str] = []
        monkeypatch.setattr(
            "dgov.lifecycle.create_worker_pane",
            lambda **kwargs: create_calls.append(kwargs["slug"]),
        )

        result = batch_dispatch(str(spec_path), session_root=str(session_root))

        assert result["merged"] == []
        assert result["failed"] == ["t1"]
        assert create_calls == []

    def test_batch_dispatch_invalid_toml_raises(self, tmp_path):
        spec = tmp_path / "bad.toml"
        spec.write_text("this is not valid toml =")  # invalid

        with pytest.raises(Exception):
            batch_dispatch(str(spec), session_root=str(tmp_path))
