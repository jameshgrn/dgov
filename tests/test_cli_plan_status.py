"""Tests for `dgov plan status` CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.deploy_log import append as deploy_append

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


def _compile_plan(runner: CliRunner, plan_dir: Path) -> None:
    """Helper: compile a plan tree via CLI (dry-run)."""
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code == 0, result.output


def _make_plan_tree(root: Path) -> Path:
    """Create a minimal plan tree with two units."""
    plan_dir = root / "testplan"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "test-plan"\nsummary = "Test"\nsections = ["core"]\n'
    )
    core_dir = plan_dir / "core"
    core_dir.mkdir()
    (core_dir / "work.toml").write_text(
        "[tasks.alpha]\n"
        'summary = "Alpha"\nprompt = "Do alpha"\ncommit_message = "alpha"\n'
        'files.create = ["a.py"]\n\n'
        "[tasks.beta]\n"
        'summary = "Beta"\nprompt = "Do beta"\ncommit_message = "beta"\n'
        'depends_on = ["alpha"]\nfiles.create = ["b.py"]\n'
    )
    return plan_dir


# -- Not compiled --


def test_status_not_compiled(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code != 0
    assert "Not compiled" in result.output


def test_status_not_compiled_json(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["status"] == "not_compiled"


# -- All pending --


def test_status_all_pending(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        # Need cwd to be the project root for deploy log lookup
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "2 total" in result.output
    assert "0 deployed" in result.output
    assert "2 pending" in result.output
    assert "○" in result.output


def test_status_all_pending_json(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["plan"] == "test-plan"
    assert data["units"] == 2
    assert data["deployed"] == 0
    assert data["pending"] == 2
    assert data["stale"] is False


# -- With deployments --


def test_status_partial_deploy(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "abc1234", "2026-04-06T12:00:00Z")
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "1 deployed" in result.output
    assert "1 pending" in result.output


def test_status_partial_deploy_json(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "abc1234", "2026-04-06T12:00:00Z")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["deployed"] == 1
    assert data["pending"] == 1
    statuses = {u["unit"]: u for u in data["unit_statuses"]}
    assert statuses["core/work.alpha"]["status"] == "deployed"
    assert statuses["core/work.alpha"]["sha"] == "abc1234"
    assert statuses["core/work.beta"]["status"] == "pending"


def test_status_blocked_by_shown(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    # beta depends on alpha; neither deployed → beta blocked by alpha
    assert "blocked by" in result.output


def test_status_blocked_by_cleared_after_deploy(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    beta = next(u for u in data["unit_statuses"] if u["unit"] == "core/work.beta")
    # alpha is deployed, so beta should not be blocked
    assert beta["blocked_by"] == ""


# -- Staleness --


def test_status_stale_detection(runner: CliRunner, tmp_path: Path) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        # Touch a source file to make it newer than the compiled plan
        time.sleep(0.1)
        source = plan_dir / "core" / "work.toml"
        source.write_text(source.read_text() + "\n# touched\n")
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "stale" in result.output.lower()


def test_status_stale_json(runner: CliRunner, tmp_path: Path) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        time.sleep(0.1)
        source = plan_dir / "core" / "work.toml"
        source.write_text(source.read_text() + "\n# touched\n")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stale"] is True


# -- help --


def test_plan_status_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["plan", "status", "--help"])
    assert result.exit_code == 0
    assert "deployment status" in result.output.lower()
