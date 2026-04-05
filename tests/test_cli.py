"""Tests for dgov CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import (
    _detect_project,
    _render_project_toml,
    cli,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    """Prevent DGOV_JSON from leaking between tests."""
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


# -- Bare invocation / status --


def test_bare_invocation_shows_status(runner: CliRunner, tmp_path: Path) -> None:
    """dgov with no args should show status, not error."""
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, [])
        assert result.exit_code == 0
        assert "status" in result.output


def test_status_subcommand(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "status" in result.output


def test_status_json(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["--json", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "status" in data
    assert "tasks" in data


# -- validate --


def test_validate_valid_plan(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[plan]\nname = "test"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
        'files.create = ["a.py"]\n'
    )
    result = runner.invoke(cli, ["validate", str(plan)])
    assert result.exit_code == 0
    assert "Validation passed" in result.output
    assert "do a" in result.output


def test_validate_conflict_plan(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[plan]\nname = "conflict"\n\n'
        "[tasks.a]\n"
        'summary = "a"\nprompt = "a"\ncommit_message = "a"\n'
        'files.edit = ["shared.py"]\n\n'
        "[tasks.b]\n"
        'summary = "b"\nprompt = "b"\ncommit_message = "b"\n'
        'files.edit = ["shared.py"]\n'
    )
    result = runner.invoke(cli, ["validate", str(plan)])
    assert result.exit_code != 0
    assert "ERROR" in result.output
    assert "File conflict" in result.output


def test_validate_json_output(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[plan]\nname = "test"\n\n[tasks.a]\nsummary = "a"\nprompt = "a"\ncommit_message = "a"\n'
    )
    result = runner.invoke(cli, ["--json", "validate", str(plan)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["valid"] is True
    assert data["tasks"] == 1


def test_validate_bad_toml(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text("not valid toml {{{{")
    result = runner.invoke(cli, ["validate", str(plan)])
    assert result.exit_code != 0


def test_validate_missing_plan_section(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text('[tasks.a]\nsummary = "a"\nprompt = "a"\ncommit_message = "a"\n')
    result = runner.invoke(cli, ["validate", str(plan)])
    assert result.exit_code != 0


def test_validate_non_toml_file(runner: CliRunner, tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    result = runner.invoke(cli, ["validate", str(plan)])
    assert result.exit_code != 0


# -- init --


def test_init_creates_project_toml(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "src").mkdir()
        Path(td, "tests").mkdir()
        Path(td, "main.py").touch()
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "Created" in result.output
        config = Path(td, ".dgov", "project.toml")
        assert config.exists()
        content = config.read_text()
        assert 'language = "python"' in content
        assert 'src_dir = "src/"' in content


def test_init_refuses_overwrite(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        dgov_dir = Path(td, ".dgov")
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("[project]\n")
        result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0
        assert "Already exists" in result.output


def test_init_force_overwrites(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        dgov_dir = Path(td, ".dgov")
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("[project]\n")
        result = runner.invoke(cli, ["init", "--force"])
        assert result.exit_code == 0
        assert "Created" in result.output


# -- _detect_project --


def test_detect_python_project(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "main.py").touch()
    lang, src, test, ext = _detect_project(tmp_path)
    assert lang == "python"
    assert src == "src/"
    assert test == "tests/"
    assert ".py" in ext


def test_detect_rust_project(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for i in range(5):
        (tmp_path / f"file{i}.rs").touch()
    lang, src, test, ext = _detect_project(tmp_path)
    assert lang == "rust"
    assert ".rs" in ext


def test_detect_fallback_to_python(tmp_path: Path) -> None:
    """Empty dir defaults to python."""
    lang, src, test, ext = _detect_project(tmp_path)
    assert lang == "python"


# -- _render_project_toml --


def test_render_project_toml() -> None:
    content = _render_project_toml("python", "src/", "tests/", [".py"])
    assert "[project]" in content
    assert 'language = "python"' in content
    assert "[conventions]" in content


# -- help / version --


def test_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "dgov" in result.output
    assert "status" in result.output
    assert "validate" in result.output
    assert "init" in result.output


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "dgov" in result.output


# -- watch --


def test_watch_subcommand_registered(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["watch", "--help"])
    assert result.exit_code == 0
    assert "Stream" in result.output
