"""Tests for `dgov delegate` CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import cli

pytestmark = pytest.mark.unit

_PROJECT_TOML = """\
[project]
language = "python"
src_dir = "src/"
test_dir = "tests/"
provider = "fireworks"
test_cmd = "uv run pytest -q {test_dir}"
lint_cmd = "uv run ruff check {file}"
format_cmd = "uv run ruff format {file}"
lint_fix_cmd = "uv run ruff check --fix {file}"
format_check_cmd = "uv run ruff format --check {file}"

[providers.fireworks]
default_agent = "accounts/fireworks/routers/kimi-k2p6-turbo"
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"

[providers.claude]
default_agent = "sonnet"
base_url = "claude-code://daily?max_turns=32"
"""


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(_PROJECT_TOML)
    return tmp_path


def test_delegate_happy_path(runner: CliRunner, project_root: Path) -> None:
    result = runner.invoke(
        cli,
        ["delegate", "implement the new feature", "--root", str(project_root)],
    )
    assert result.exit_code == 0, result.output
    assert "DELEGATION BRIEF" in result.output
    assert "implement the new feature" in result.output
    assert "LIEUTENANT GOVERNOR" in result.output
    assert "fireworks" in result.output
    assert "STOP RULES" in result.output
    assert "REQUIRED WORKFLOW" in result.output
    assert "VERIFICATION EXPECTATIONS" in result.output
    assert "LEDGER OBLIGATIONS" in result.output


def test_delegate_explicit_providers(runner: CliRunner, project_root: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "delegate",
            "refactor the persistence layer",
            "--lieutenant-provider",
            "claude",
            "--worker-provider",
            "fireworks",
            "--root",
            str(project_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert "fireworks" in result.output


def test_delegate_unknown_lieutenant_provider_fails(runner: CliRunner, project_root: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "delegate",
            "do something",
            "--lieutenant-provider",
            "unknown-provider",
            "--root",
            str(project_root),
        ],
    )
    assert result.exit_code == 1
    assert "unknown-provider" in result.output


def test_delegate_unknown_worker_provider_fails(runner: CliRunner, project_root: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "delegate",
            "do something",
            "--worker-provider",
            "no-such-provider",
            "--root",
            str(project_root),
        ],
    )
    assert result.exit_code == 1
    assert "no-such-provider" in result.output


def test_delegate_json_output(runner: CliRunner, project_root: Path) -> None:
    result = runner.invoke(
        cli,
        ["--json", "delegate", "build the feature", "--root", str(project_root)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["vision"] == "build the feature"
    assert "lieutenant" in payload
    assert payload["lieutenant"]["provider"] == "fireworks"
    assert "worker" in payload
    assert "stop_rules" in payload
    assert isinstance(payload["stop_rules"], list)
    assert len(payload["stop_rules"]) > 0
    assert "workflow" in payload
    assert "verification" in payload
    assert "lint" in payload["verification"]
    assert "ledger_obligations" in payload
