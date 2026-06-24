"""Tests for `dgov plan create` settings resolution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import click
import pytest
from click.testing import CliRunner

from dgov.cli.plan_create import (
    _materialize_plan,
    _plan_create_settings,
    _planner_command,
    plan_create_cmd,
)

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
        provider=None,
    )

    payload = json.loads(config_json)
    assert agent == "provider/model"
    assert payload["llm_provider"] == "test-provider"
    assert interactive is False


def test_plan_create_provider_override_selects_planner_provider(tmp_path: Path) -> None:
    _write_project_toml(
        tmp_path,
        """
[project]
provider = "local"

[providers.local]
default_agent = "gemma"
base_url = "http://localhost:8080/v1"
api_key_env = "LOCAL_LLM_API_KEY"

[providers.claude-sonnet-plan]
default_agent = "sonnet"
base_url = "claude-code://daily?preset=plan&max_turns=32"
""",
    )

    agent, config_json, interactive = _plan_create_settings(
        tmp_path,
        model=None,
        autonomous=True,
        provider="claude-sonnet-plan",
    )

    payload = json.loads(config_json)
    assert agent == "sonnet"
    assert payload["llm_provider"] == "claude-sonnet-plan"
    assert payload["llm_base_url"] == "claude-code://daily?preset=plan&max_turns=32"
    assert payload["llm_api_key_env"] == ""
    assert interactive is False


def test_materialize_plan_writes_default_and_task_providers(tmp_path: Path) -> None:
    plan_dir = _materialize_plan(
        {
            "name": "plan-for-local",
            "summary": "Plan for local.",
            "tasks": [
                {
                    "slug": "gemma-task",
                    "summary": "Use the default local provider",
                    "prompt": "Orient:\nRead.\n\nEdit:\n1. Change.\n\nVerify:\n- Check.",
                    "commit_message": "Change local task",
                    "files": {"edit": ["src/example.py"]},
                },
                {
                    "slug": "haiku-task",
                    "summary": "Override to Haiku",
                    "prompt": "Orient:\nRead.\n\nEdit:\n1. Change.\n\nVerify:\n- Check.",
                    "commit_message": "Change haiku task",
                    "provider": "claude-haiku-worker",
                    "files": {"edit": ["tests/test_example.py"]},
                },
            ],
        },
        tmp_path,
        default_provider="local",
    )

    root_toml = (plan_dir / "_root.toml").read_text()
    tasks_toml = (plan_dir / "tasks" / "main.toml").read_text()
    assert 'default_provider = "local"' in root_toml
    assert "[tasks.gemma-task]" in tasks_toml
    assert "[tasks.haiku-task]" in tasks_toml
    assert 'provider = "claude-haiku-worker"' in tasks_toml


def test_planner_command_passes_target_provider() -> None:
    command = _planner_command(
        "/repo",
        "make a plan",
        "sonnet",
        False,
        "{}",
        target_provider="local",
    )

    assert command[command.index("--target-provider") + 1] == "local"


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


def test_goal_file_reads_exact_multiline_text_with_backticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal_text = 'Add `--foo` and `--bar` flags\nwith backticks and "quotes"\nline three\n'
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text(goal_text, encoding="utf-8")

    captured: dict = {}

    def _fake_execute(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("dgov.cli.plan_create._execute_plan_create", _fake_execute)

    result = CliRunner().invoke(plan_create_cmd, ["--goal-file", str(goal_file)])
    assert result.exit_code == 0, result.output
    assert captured["goal"] == goal_text


def test_positional_goal_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _fake_execute(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("dgov.cli.plan_create._execute_plan_create", _fake_execute)

    result = CliRunner().invoke(plan_create_cmd, ["add a feature"])
    assert result.exit_code == 0, result.output
    assert captured["goal"] == "add a feature"


def test_goal_file_and_positional_together_fail(tmp_path: Path) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("file goal")

    result = CliRunner().invoke(
        plan_create_cmd, ["--goal-file", str(goal_file), "positional goal"]
    )
    assert result.exit_code == 2
    assert "not both" in result.output


def test_positional_and_goal_file_together_fail(tmp_path: Path) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("file goal")

    result = CliRunner().invoke(
        plan_create_cmd, ["positional goal", "--goal-file", str(goal_file)]
    )
    assert result.exit_code == 2
    assert "not both" in result.output


def test_no_goal_source_fails() -> None:
    result = CliRunner().invoke(plan_create_cmd, [])
    assert result.exit_code == 2
    assert "--goal-file" in result.output


def test_help_shows_goal_file_option() -> None:
    result = CliRunner().invoke(plan_create_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--goal-file" in result.output
