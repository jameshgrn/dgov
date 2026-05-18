"""Tests for `dgov plan list` CLI command."""

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
def runner():
    return CliRunner()


def _write_compiled_plan(plan_dir: Path, *, plan_name: str, unit_count: int) -> None:
    plan_dir.mkdir(parents=True)
    lines = [f'[plan]\nname = "{plan_name}"\n']
    for i in range(unit_count):
        lines.append(
            f'\n[tasks."tasks/main.t{i}"]\nsummary = "x"\nprompt = "y"\ncommit_message = "z"\n'
        )
    (plan_dir / "_compiled.toml").write_text("".join(lines))


def _write_uncompiled_plan(plan_dir: Path, *, plan_name: str) -> None:
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(f'[plan]\nname = "{plan_name}"\n')


def _make_project_root(tmp_path: Path) -> Path:
    (tmp_path / ".dgov").mkdir()
    return tmp_path


def test_list_no_plans_dir(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    monkeypatch.chdir(root)
    result = runner.invoke(cli, ["plan", "list"])
    assert result.exit_code == 0
    assert "No plans directory" in result.output


def test_list_empty_plans_dir(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    (root / ".dgov" / "plans").mkdir()
    monkeypatch.chdir(root)
    result = runner.invoke(cli, ["plan", "list"])
    assert result.exit_code == 0
    assert "No plans found" in result.output


def test_list_shows_active_only_by_default(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_compiled_plan(plans / "alpha", plan_name="alpha", unit_count=1)
    _write_compiled_plan(plans / "archive" / "old", plan_name="old", unit_count=1)
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list"])

    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "old" not in result.output
    assert "active" in result.output


def test_list_all_includes_archive(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_compiled_plan(plans / "alpha", plan_name="alpha", unit_count=1)
    _write_compiled_plan(plans / "archive" / "old", plan_name="old", unit_count=1)
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list", "--all"])

    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "old" in result.output
    assert "active" in result.output
    assert "archive" in result.output


def test_list_archived_only(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_compiled_plan(plans / "alpha", plan_name="alpha", unit_count=1)
    _write_compiled_plan(plans / "archive" / "old", plan_name="old", unit_count=1)
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list", "--archived"])

    assert result.exit_code == 0
    assert "alpha" not in result.output
    assert "old" in result.output


def test_list_rejects_all_and_archived_together(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    root = _make_project_root(tmp_path)
    (root / ".dgov" / "plans").mkdir()
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list", "--all", "--archived"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_list_marks_uncompiled(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_uncompiled_plan(plans / "draft", plan_name="draft")
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list"])

    assert result.exit_code == 0
    assert "draft" in result.output
    assert "uncompiled" in result.output


def test_list_skips_underscore_dirs(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_compiled_plan(plans / "alpha", plan_name="alpha", unit_count=1)
    (plans / "_scratch").mkdir(parents=True)
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["plan", "list"])

    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "_scratch" not in result.output


def test_list_json_output_compiled_plan(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_compiled_plan(plans / "alpha", plan_name="alpha", unit_count=2)
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["--json", "plan", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["name"] == "alpha"
    assert entry["archived"] is False
    assert entry["compiled"] is True
    assert entry["total"] == 2
    assert entry["deployed"] == 0
    assert entry["status"] == "compiled"


def test_list_json_output_no_plans_dir(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    monkeypatch.chdir(root)
    result = runner.invoke(cli, ["--json", "plan", "list"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_list_json_output_uncompiled_plan(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    _write_uncompiled_plan(plans / "draft", plan_name="draft")
    monkeypatch.chdir(root)

    result = runner.invoke(cli, ["--json", "plan", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0] == {
        "name": "draft",
        "path": str(plans / "draft"),
        "archived": False,
        "compiled": False,
        "total": 0,
        "deployed": 0,
        "status": "uncompiled",
    }
