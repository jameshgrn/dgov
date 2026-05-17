"""Tests for `dgov plan create` settings resolution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import click
import pytest

from dgov.cli.plan_create import _plan_create_settings

pytestmark = pytest.mark.unit


def _write_project_toml(root: Path, content: str) -> None:
    dgov_dir = root / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(content)


def test_plan_create_requires_planner_model_when_provider_has_no_default(
    tmp_path: Path,
) -> None:
    _write_project_toml(
        tmp_path,
        """
[project]
provider = "test-provider"

[providers.test-provider]
base_url = "https://provider.test/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
""",
    )

    with pytest.raises(click.ClickException, match="Planner model is not configured"):
        _plan_create_settings(tmp_path, model=None, autonomous=True)


def test_plan_create_model_override_supplies_planner_model(tmp_path: Path) -> None:
    _write_project_toml(
        tmp_path,
        """
[project]
provider = "test-provider"

[providers.test-provider]
base_url = "https://provider.test/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
""",
    )

    agent, config_json, interactive = _plan_create_settings(
        tmp_path,
        model="provider/model",
        autonomous=True,
    )

    payload = json.loads(config_json)
    assert agent == "provider/model"
    assert payload["llm_provider"] == "test-provider"
    assert interactive is False


def test_plan_create_reports_missing_provider_config(tmp_path: Path) -> None:
    _write_project_toml(
        tmp_path,
        """
[project]
default_agent = "provider/model"
provider = "missing"
""",
    )

    with pytest.raises(click.ClickException, match="Planner provider is not configured"):
        _plan_create_settings(tmp_path, model=None, autonomous=True)


def test_planner_reports_invalid_project_config_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.planner import _planner_config_and_provider

    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    with pytest.raises(SystemExit) as excinfo:
        _planner_config_and_provider(tmp_path, "{not-json")

    assert excinfo.value.code == 1
    assert events == [
        (
            "error",
            "Project configuration error: Invalid worker project config JSON: "
            "Expecting property name enclosed in double quotes",
        )
    ]


def test_planner_stdin_non_object_json_falls_back_to_raw_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.planner import _ask_user_via_stdin

    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    class _Stdin:
        @staticmethod
        def readline() -> str:
            return '["not-an-answer"]\n'

    monkeypatch.setattr("sys.stdin", _Stdin())

    assert _ask_user_via_stdin("Question?") == '["not-an-answer"]'
    assert events == [("question", "Question?")]


def test_read_planner_event_ignores_non_object_json() -> None:
    from dgov.cli.plan_create import _read_planner_event

    class _Stdout:
        @staticmethod
        async def readline() -> bytes:
            return b'["not-an-event"]\n'

    class _Proc:
        stdout = _Stdout()

    assert asyncio.run(_read_planner_event(cast(Any, _Proc()))) == {}
