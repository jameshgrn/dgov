"""Tests for headless worker role dispatch."""

import asyncio
import json
from pathlib import Path

import pytest

from dgov.dag_parser import DagFileSpec, DagTaskSpec
from dgov.workers.headless import _config_json_for_task, _script_for_role, run_headless_worker


def test_script_for_worker_role() -> None:
    script = _script_for_role("worker")
    assert isinstance(script, Path)
    assert script.name == "worker.py"


def test_script_for_researcher_role() -> None:
    script = _script_for_role("researcher")
    assert isinstance(script, Path)
    assert script.name == "researcher.py"


def test_script_for_role_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown task role"):
        _script_for_role("mystery")


def test_run_headless_worker_uses_project_config_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\ntype_check_cmd = "uv run ty check"\nline_length = 120\n'
    )
    task = DagTaskSpec(
        slug="t1",
        summary="test",
        prompt="do it",
        commit_message="test: task",
        agent="test-agent",
        files=DagFileSpec(create=("x.py",)),
    )
    captured: dict[str, object] = {}

    class _Stdout:
        async def readline(self) -> bytes:
            return b""

    class _Process:
        stdout = _Stdout()

        async def wait(self) -> int:
            return 0

    async def _mock_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    exits: list[tuple[int, str, int, int]] = []

    def _on_exit(
        task_slug: str,
        pane_slug: str,
        exit_code: int,
        last_error: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        exits.append((exit_code, last_error, prompt_tokens, completion_tokens))

    asyncio.run(
        run_headless_worker(
            project_root=str(tmp_path),
            plan_name="plan-1",
            task_slug="t1",
            pane_slug="pane-1",
            worktree_path=tmp_path,
            task=task,
            task_scope={"task_slug": "t1", "create": ["x.py"]},
            on_exit=_on_exit,
        )
    )

    args = captured.get("args")
    assert isinstance(args, tuple)
    config_json = args[-3]
    assert isinstance(config_json, str)
    payload = json.loads(config_json)
    assert payload["type_check_cmd"] == "uv run ty check"
    assert payload["line_length"] == 120
    task_scope_json = args[-1]
    assert isinstance(task_scope_json, str)
    assert json.loads(task_scope_json)["create"] == ["x.py"]
    assert exits == [(0, "", 0, 0)]


def test_config_json_for_task_applies_iteration_budget_override(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text("[project]\nworker_iteration_budget = 50\n")
    task = DagTaskSpec(
        slug="t1",
        summary="test",
        prompt="do it",
        commit_message="test: task",
        agent="test-agent",
        iteration_budget=9,
        files=DagFileSpec(create=("x.py",)),
    )

    payload = json.loads(_config_json_for_task(str(tmp_path), task))

    assert payload["worker_iteration_budget"] == 9


def test_run_headless_worker_emits_plan_name_on_worker_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = DagTaskSpec(
        slug="t1",
        summary="test",
        prompt="do it",
        commit_message="test: task",
        agent="test-agent",
        files=DagFileSpec(create=("x.py",)),
    )
    emitted: list = []

    class _Stdout:
        def __init__(self) -> None:
            self._lines = [
                b'{"worker_event":{"type":"thought","content":"hi"}}\n',
                b"",
            ]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class _Process:
        stdout = _Stdout()

        async def wait(self) -> int:
            return 0

    async def _mock_create_subprocess_exec(*args, **kwargs):
        return _Process()

    def _capture_emit(session_root, event, pane="", **kwargs):
        emitted.append(event)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)
    monkeypatch.setattr("dgov.workers.headless.emit_event", _capture_emit)

    asyncio.run(
        run_headless_worker(
            project_root=str(tmp_path),
            plan_name="plan-1",
            task_slug="t1",
            pane_slug="pane-1",
            worktree_path=tmp_path,
            task=task,
            task_scope={"task_slug": "t1", "create": ["x.py"]},
            on_exit=lambda *_args: None,
        )
    )

    assert len(emitted) == 1
    evt = emitted[0]
    assert evt.event_type == "worker_log"
    assert evt.pane == "pane-1"
    assert evt.plan_name == "plan-1"
    assert evt.task_slug == "t1"
    assert evt.log_type == "thought"
    assert evt.content == "hi"
