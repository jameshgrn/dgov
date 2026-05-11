"""Tests for headless worker role dispatch."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from dgov.dag_parser import DagFileSpec, DagTaskSpec
from dgov.workers.headless import _config_json_for_task, _script_for_role, run_headless_worker


def _fake_subprocess_mock(exit_code: int = 0, stdout_lines: list[bytes] | None = None):
    """Create a mock subprocess that yields stdout_lines then exits with exit_code."""
    lines = (stdout_lines or [b""]) + [b""]

    class _Stdout:
        def __init__(self) -> None:
            self._lines = lines[:]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class _Process:
        stdout = _Stdout()

        async def wait(self) -> int:
            return exit_code

    return _Process()


def _make_mock_subprocess_exec(
    exit_code: int = 0,
    stdout_lines: list[bytes] | None = None,
    capture: dict[str, object] | None = None,
) -> Callable:
    """Return an async function suitable for monkeypatching create_subprocess_exec."""

    async def _mock(*args, **kwargs):
        if capture is not None:
            capture["args"] = args
        return _fake_subprocess_mock(exit_code=exit_code, stdout_lines=stdout_lines)

    return _mock


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


def _make_exit_recorder() -> tuple[list[tuple[int, str, int, int]], Callable]:
    """Return a list to collect exit calls and a callback that appends to it."""
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

    return exits, _on_exit


def _make_test_task(
    slug: str = "t1",
    summary: str = "test",
    prompt: str = "do it",
    commit_message: str = "test: task",
    agent: str = "test-agent",
    files: DagFileSpec | None = None,
    iteration_budget: int | None = None,
) -> DagTaskSpec:
    """Create a DagTaskSpec with sensible defaults; override via kwargs."""
    if files is None:
        files = DagFileSpec(create=("x.py",))
    return DagTaskSpec(
        slug=slug,
        summary=summary,
        prompt=prompt,
        commit_message=commit_message,
        agent=agent,
        files=files,
        iteration_budget=iteration_budget,
    )


def _setup_project_toml(tmp_path: Path, content: str) -> None:
    """Create .dgov dir and write project.toml with the given content."""
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(content)


def test_run_headless_worker_uses_project_config_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_project_toml(
        tmp_path, '[project]\ntype_check_cmd = "uv run ty check"\nline_length = 120\n'
    )
    task = _make_test_task()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_mock_subprocess_exec(exit_code=0, capture=captured),
    )
    exits, on_exit = _make_exit_recorder()

    asyncio.run(
        run_headless_worker(
            project_root=str(tmp_path),
            plan_name="plan-1",
            task_slug="t1",
            pane_slug="pane-1",
            worktree_path=tmp_path,
            task=task,
            task_scope={"task_slug": "t1", "create": ["x.py"]},
            on_exit=on_exit,
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
    _setup_project_toml(tmp_path, "[project]\nworker_iteration_budget = 50\n")
    task = _make_test_task(iteration_budget=9)

    payload = json.loads(_config_json_for_task(str(tmp_path), task))

    assert payload["worker_iteration_budget"] == 9


def test_run_headless_worker_emits_plan_name_on_worker_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _make_test_task()
    emitted: list = []

    def _capture_emit(session_root, event, pane="", **kwargs):
        emitted.append(event)

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_mock_subprocess_exec(
            exit_code=0,
            stdout_lines=[b'{"worker_event":{"type":"thought","content":"hi"}}\n'],
        ),
    )
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
