"""Tests for `dgov diagnose` CLI commands."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import cli

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


def test_diagnose_allows_ignored_plan_archive(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / ".gitignore").write_text("plans/archive/\n")
    monkeypatch.setattr("dgov.cli.diagnose.read_events", lambda *a, **k: [])
    result = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No failure shapes matched" in result.output


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


def test_diagnose_reports_stale_review_attention(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".dgov").mkdir()
    monkeypatch.setattr("dgov.cli.diagnose.read_events", lambda *a, **k: [])
    monkeypatch.setattr(
        "dgov.cli.diagnose.tasks_from_events",
        lambda *a, **k: [
            {
                "plan_name": "archived-plan",
                "slug": "tasks/a",
                "state": "reviewed_fail",
            }
        ],
    )
    monkeypatch.setattr("dgov.cli.diagnose.active_plan_source_names", lambda *_a: frozenset())

    result = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "stale_review_attention" in result.output
    assert "archived-plan/tasks/a" in result.output


def _record_archived_review_failure(project_root: Path) -> None:
    from dgov.persistence import emit_event

    emit_event(str(project_root), "run_start", "run-plan", plan_name="archived-plan")
    for event_name in ("dag_task_dispatched", "task_done"):
        emit_event(
            str(project_root),
            event_name,
            "pane-a",
            plan_name="archived-plan",
            task_slug="tasks/a",
        )
    emit_event(
        str(project_root),
        "review_fail",
        "pane-a",
        plan_name="archived-plan",
        task_slug="tasks/a",
        verdict="lint_fail",
    )


def test_diagnose_repairs_stale_review_attention(runner: CliRunner, tmp_path: Path) -> None:
    from dgov.live_state import tasks_from_events
    from dgov.persistence import read_events

    _init_repo(tmp_path)
    (tmp_path / ".dgov").mkdir()
    _record_archived_review_failure(tmp_path)

    before = runner.invoke(cli, ["diagnose", "--root", str(tmp_path)])
    repaired = runner.invoke(
        cli,
        [
            "diagnose",
            "--root",
            str(tmp_path),
            "--repair-stale-review-attention",
        ],
    )

    events = read_events(str(tmp_path))
    closed_events = [event for event in events if event["event"] == "task_closed"]

    assert before.exit_code == 0, before.output
    assert "stale_review_attention" in before.output
    assert repaired.exit_code == 0, repaired.output
    assert "Closed 1 stale reviewed task(s)." in repaired.output
    assert "No failure shapes matched current repo state." in repaired.output
    assert len(closed_events) == 1
    assert closed_events[0]["pane"] == "diagnose"
    assert closed_events[0]["plan_name"] == "archived-plan"
    assert closed_events[0]["task_slug"] == "tasks/a"
    assert closed_events[0]["reason"] == "stale_review_attention"
    assert tasks_from_events(str(tmp_path), latest_run_only=True) == [
        {"slug": "tasks/a", "state": "closed", "plan_name": "archived-plan"}
    ]


def test_diagnose_json_output(
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
    result = runner.invoke(cli, ["--json", "diagnose", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload["findings"], list)
    assert len(payload["findings"]) >= 1
    assert "name" in payload["findings"][0]
