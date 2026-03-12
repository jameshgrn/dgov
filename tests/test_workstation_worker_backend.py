"""Unit tests for workstation TmuxWorkerBackend adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dgov.models import TaskSpec
from dgov.worker_backend import PaneHandle, TmuxWorkerBackend

pytestmark = pytest.mark.unit


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        description="desc",
        exports=[],
        imports=[],
        touches=[],
        body="body",
    )


def test_spawn_creates_worktree_when_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = TmuxWorkerBackend(session_root="/tmp/session-root")
    worktree_path = tmp_path / ".dmux" / "worktrees" / "W01"
    captured: dict[str, object] = {}

    def fake_create_worker_pane(**kwargs: object):
        captured.update(kwargs)
        return type("Pane", (), {"slug": "W01"})()

    monkeypatch.setattr("dgov.worker_backend.create_worker_pane", fake_create_worker_pane)

    handle = asyncio.run(
        backend.spawn(_task("W01"), worktree_path, {"DISTRIBUTARY_TASK_FILE": "x"})
    )

    assert isinstance(handle, PaneHandle)
    assert handle.slug == "W01"
    assert handle.project_root == str(tmp_path)
    assert "existing_worktree" not in captured


def test_spawn_reuses_existing_worktree_when_path_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = TmuxWorkerBackend(session_root="/tmp/session-root")
    worktree_path = tmp_path / ".distributary" / "worktrees" / "W01"
    worktree_path.mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_create_worker_pane(**kwargs: object):
        captured.update(kwargs)
        return type("Pane", (), {"slug": "W01"})()

    monkeypatch.setattr("dgov.worker_backend.create_worker_pane", fake_create_worker_pane)

    handle = asyncio.run(
        backend.spawn(_task("W01"), worktree_path, {"DISTRIBUTARY_TASK_FILE": "x"})
    )

    assert isinstance(handle, PaneHandle)
    assert captured["existing_worktree"] == str(worktree_path)


def test_cleanup_uses_handle_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TmuxWorkerBackend(session_root="/tmp/session-root")
    captured: dict[str, object] = {}

    def fake_close_worker_pane(
        project_root: str, slug: str, session_root: str | None = None
    ) -> bool:
        captured["project_root"] = project_root
        captured["slug"] = slug
        captured["session_root"] = session_root
        return True

    monkeypatch.setattr("dgov.worker_backend.close_worker_pane", fake_close_worker_pane)

    asyncio.run(backend.cleanup(PaneHandle(slug="W01", project_root="/tmp/repo")))

    assert captured == {
        "project_root": "/tmp/repo",
        "slug": "W01",
        "session_root": "/tmp/session-root",
    }


def test_cleanup_string_handle_falls_back_to_session_root(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TmuxWorkerBackend(session_root="/tmp/session-root")
    captured: dict[str, object] = {}

    def fake_close_worker_pane(
        project_root: str, slug: str, session_root: str | None = None
    ) -> bool:
        captured["project_root"] = project_root
        captured["slug"] = slug
        captured["session_root"] = session_root
        return True

    monkeypatch.setattr("dgov.worker_backend.close_worker_pane", fake_close_worker_pane)

    asyncio.run(backend.cleanup("W01"))

    assert captured == {
        "project_root": "/tmp/session-root",
        "slug": "W01",
        "session_root": "/tmp/session-root",
    }


# ---------------------------------------------------------------------------
# PaneHandle
# ---------------------------------------------------------------------------


class TestPaneHandle:
    def test_fields(self) -> None:
        h = PaneHandle(slug="my-task", project_root="/tmp/repo")
        assert h.slug == "my-task"
        assert h.project_root == "/tmp/repo"

    def test_equality(self) -> None:
        h1 = PaneHandle(slug="a", project_root="/b")
        h2 = PaneHandle(slug="a", project_root="/b")
        assert h1 == h2


# ---------------------------------------------------------------------------
# _resolve_agent
# ---------------------------------------------------------------------------


class TestResolveAgent:
    def test_default_is_pi(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        agent, flags = backend._resolve_agent(task)
        assert agent == "pi"
        assert flags == ""

    def test_worker_cmd_claude(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        task.worker_cmd = "claude --dangerously-skip-permissions"
        agent, flags = backend._resolve_agent(task)
        assert agent == "claude"

    def test_worker_cmd_codex(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        task.worker_cmd = "codex"
        agent, flags = backend._resolve_agent(task)
        assert agent == "codex"

    def test_worker_cmd_gemini(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        task.worker_cmd = "gemini"
        agent, flags = backend._resolve_agent(task)
        assert agent == "gemini"

    def test_provider_uses_pi(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        task.provider = "river-gpu0"
        agent, flags = backend._resolve_agent(task)
        assert agent == "pi"
        assert "river-gpu0" in flags

    def test_unknown_worker_cmd(self) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp")
        task = _task("W01")
        task.worker_cmd = "custom-agent --flag"
        agent, flags = backend._resolve_agent(task)
        assert agent == "custom-agent"


# ---------------------------------------------------------------------------
# _extract_slug
# ---------------------------------------------------------------------------


class TestExtractSlug:
    def test_from_pane_handle(self) -> None:
        h = PaneHandle(slug="my-task", project_root="/tmp")
        assert TmuxWorkerBackend._extract_slug(h) == "my-task"

    def test_from_string(self) -> None:
        assert TmuxWorkerBackend._extract_slug("my-task") == "my-task"

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError, match="Unsupported"):
            TmuxWorkerBackend._extract_slug(42)

    def test_from_dict_raises(self) -> None:
        with pytest.raises(TypeError, match="Unsupported"):
            TmuxWorkerBackend._extract_slug({"slug": "x"})


# ---------------------------------------------------------------------------
# TmuxWorkerBackend constructor
# ---------------------------------------------------------------------------


class TestBackendConstructor:
    def test_defaults(self) -> None:
        b = TmuxWorkerBackend(session_root="/tmp/sess")
        assert b.session_root == "/tmp/sess"
        assert b.worktree_dir == ".workstation/worktrees"
        assert b.stable_threshold == 15
        assert b.poll_interval == 3

    def test_custom_params(self) -> None:
        b = TmuxWorkerBackend(
            session_root="/tmp/sess",
            worktree_dir=".custom/wt",
            stable_threshold=30,
            poll_interval=5,
        )
        assert b.worktree_dir == ".custom/wt"
        assert b.stable_threshold == 30
        assert b.poll_interval == 5


# ---------------------------------------------------------------------------
# wait() logic
# ---------------------------------------------------------------------------


class TestWait:
    def test_returns_true_when_done_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp", poll_interval=0)
        monkeypatch.setattr(
            "dgov.worker_backend._is_done",
            lambda *args, **kwargs: True,
        )
        result = asyncio.run(backend.wait(PaneHandle(slug="W01", project_root="/tmp"), timeout=10))
        assert result is True

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp", poll_interval=0, stable_threshold=999)
        monkeypatch.setattr(
            "dgov.worker_backend._is_done",
            lambda *args, **kwargs: False,
        )
        monkeypatch.setattr(
            "dgov.worker_backend.capture_worker_output",
            lambda *args, **kwargs: "changing output " + str(asyncio.get_event_loop().time()),
        )
        result = asyncio.run(backend.wait(PaneHandle(slug="W01", project_root="/tmp"), timeout=0))
        assert result is False


# ---------------------------------------------------------------------------
# spawn env filtering
# ---------------------------------------------------------------------------


class TestSpawnEnvFiltering:
    def test_only_distributary_env_vars_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = TmuxWorkerBackend(session_root="/tmp/session-root")
        worktree_path = tmp_path / ".dmux" / "worktrees" / "W01"
        captured: dict[str, object] = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return type("Pane", (), {"slug": "W01"})()

        monkeypatch.setattr("dgov.worker_backend.create_worker_pane", fake_create)

        env = {
            "DISTRIBUTARY_TASK_FILE": "x.md",
            "DISTRIBUTARY_RUN_ID": "run-1",
            "HOME": "/Users/test",
            "PATH": "/usr/bin",
        }
        asyncio.run(backend.spawn(_task("W01"), worktree_path, env))

        passed_env = captured["env_vars"]
        assert "DISTRIBUTARY_TASK_FILE" in passed_env
        assert "DISTRIBUTARY_RUN_ID" in passed_env
        assert "HOME" not in passed_env
        assert "PATH" not in passed_env
