"""Tests for `dgov verify` CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_project_toml(root: Path, content: str) -> None:
    dgov_dir = root / ".dgov"
    dgov_dir.mkdir(parents=True, exist_ok=True)
    (dgov_dir / "project.toml").write_text(content, encoding="utf-8")


def test_verify_list_empty(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(root, "")
        result = runner.invoke(cli, ["verify", "list"])

    assert result.exit_code == 0, result.output
    assert "No verification recipes configured." in result.output


def test_verify_list_shows_recipes(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(
            root,
            '[verify.check]\ncommand = "echo hello"\ndescription = "A simple check"\n',
        )
        result = runner.invoke(cli, ["verify", "list"])

    assert result.exit_code == 0, result.output
    assert "check" in result.output
    assert "A simple check" in result.output


def test_verify_list_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(
            root,
            '[verify.check]\ncommand = "echo hello"\ndescription = "A simple check"\n',
        )
        result = runner.invoke(cli, ["--json", "verify", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["recipes"][0]["name"] == "check"
    assert payload["recipes"][0]["description"] == "A simple check"
    assert payload["recipes"][0]["command"] == "echo hello"


def test_verify_run_success(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(
            root,
            '[verify.ok]\ncommand = "echo success"\ndescription = "Always passes"\n',
        )
        result = runner.invoke(cli, ["verify", "run", "ok"])

    assert result.exit_code == 0, result.output
    assert "PASS: ok" in result.output
    assert "exit_code: 0" in result.output
    assert "warnings: 0" in result.output
    assert "log:" in result.output


def test_verify_run_failure(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(
            root,
            '[verify.bad]\ncommand = "exit 1"\ndescription = "Always fails"\n',
        )
        result = runner.invoke(cli, ["verify", "run", "bad"])

    assert result.exit_code == 1, result.output
    assert "FAIL: bad" in result.output
    assert "exit_code: 1" in result.output


def test_verify_run_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(
            root,
            '[verify.ok]\ncommand = "echo success"\n',
        )
        result = runner.invoke(cli, ["--json", "verify", "run", "ok"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "pass"
    assert payload["recipe"] == "ok"
    assert payload["results"][0]["exit_code"] == 0
    assert payload["results"][0]["warning_count"] == 0
    assert payload["results"][0]["log_path"] is not None


def test_verify_run_missing_recipe(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_project_toml(root, "")
        result = runner.invoke(cli, ["verify", "run", "missing"])

    assert result.exit_code == 1, result.output
    assert "unknown verify recipe 'missing'" in result.output
