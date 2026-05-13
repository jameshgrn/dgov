"""Tests for `dgov scope status` CLI command."""

from __future__ import annotations

import json
import os
import subprocess
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


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=path, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _make_plan_dir(root: Path) -> Path:
    plan_dir = root / "plans" / "my-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "my-plan"\nsummary = "Test"\nsections = ["tasks"]\n'
    )
    tasks_dir = plan_dir / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "main.toml").write_text(
        "[tasks.alpha]\n"
        'summary = "Alpha"\n'
        'prompt = "Do alpha"\n'
        'commit_message = "alpha"\n'
        'files.create = ["src/new.py"]\n'
        'files.read = ["src/old.py"]\n'
    )
    return plan_dir


def _write_compiled_plan(plan_dir: Path) -> None:
    compiled = plan_dir / "_compiled.toml"
    compiled.write_text(
        '[plan]\nname = "my-plan"\n\n'
        "[tasks.alpha]\n"
        'summary = "Alpha"\n'
        'prompt = "Do alpha"\n'
        'commit_message = "alpha"\n'
        'files.create = ["src/new.py"]\n'
        'files.read = ["src/old.py"]\n'
    )


def test_clean_scope(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        ["scope", "status", "--task", "t", "--claim", "src/a.py"],
    )
    assert result.exit_code == 0, result.output
    assert "claimed_writable: src/a.py" in result.output
    assert "modified_files: src/a.py" in result.output
    assert "blocking: (none)" in result.output


def test_unclaimed_modified_file(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        ["scope", "status", "--task", "t", "--claim", "src/other.py"],
    )
    assert result.exit_code == 1, result.output
    assert "unclaimed_modified: src/a.py" in result.output
    assert "blocking:" in result.output


def test_read_scope_violation(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        [
            "scope",
            "status",
            "--task",
            "t",
            "--claim",
            "src/other.py",
            "--read",
            "src/a.py",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "read_scope_violation" in result.output or "read-only" in result.output
    assert "blocking:" in result.output


def test_plan_derived_claims(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    plan_dir = _make_plan_dir(tmp_path)
    _write_compiled_plan(plan_dir)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "plan"], cwd=tmp_path, check=True)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "new.py").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        ["scope", "status", "--task", "alpha", "--plan", str(plan_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "claimed_writable: src/new.py" in result.output
    assert "claimed_readonly: src/old.py" in result.output
    assert "blocking: (none)" in result.output


def test_json_output(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        ["--json", "scope", "status", "--task", "t", "--claim", "src/a.py"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["claimed_writable"] == ["src/a.py"]
    assert data["modified_files"] == ["src/a.py"]
    assert data["blocking_failure"] is None
