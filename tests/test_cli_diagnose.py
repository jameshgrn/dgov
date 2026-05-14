"""Tests for `dgov diagnose` CLI commands."""

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
def runner() -> CliRunner:
    return CliRunner()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def test_diagnose_clean_repo(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".dgov").mkdir()
    monkeypatch.setattr("dgov.cli.diagnose.read_events", lambda *a, **k: [])
    result = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No failure shapes matched" in result.output


def test_diagnose_reports_archive_drift(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / ".gitignore").write_text("plans/archive/\n")
    monkeypatch.setattr("dgov.cli.diagnose.read_events", lambda *a, **k: [])
    result = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "archive_policy_drift" in result.output


def test_diagnose_reports_scope_violation(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".dgov").mkdir()
    monkeypatch.setattr(
        "dgov.cli.diagnose.read_events",
        lambda *a, **k: [
            {
                "event": "review_fail",
                "verdict": "scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            }
        ],
    )
    result = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "plan_claims_violation" in result.output
    assert "p/t" in result.output


def test_diagnose_json_output(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / ".gitignore").write_text("plans/archive/\n")
    monkeypatch.setattr("dgov.cli.diagnose.read_events", lambda *a, **k: [])
    result = runner.invoke(cli, ["--json", "diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload["findings"], list)
    assert len(payload["findings"]) >= 1
    assert "name" in payload["findings"][0]
