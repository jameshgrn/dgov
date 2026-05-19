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
        "stale": False,
        "run_status": None,
        "remediation_needed": False,
    }


def _compile_plan(runner: CliRunner, plan_dir: Path) -> None:
    """Helper: compile a plan tree via CLI (dry-run)."""
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code == 0, result.output


def _make_plan_tree_under_plans(plans: Path, name: str) -> Path:
    """Create a minimal plan tree with two units under the plans directory."""
    plan_dir = plans / name
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        f'[plan]\nname = "{name}"\nsummary = "Test"\nsections = ["core"]\n'
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


def _patched_run_envelope(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Stub plan_review.load_run_envelope so list tests can control run-level fields."""
    from dgov.plan_review import RunEnvelope

    envelope = overrides.get(
        "envelope",
        RunEnvelope(plan_name="test-plan", last_run_ts=None),
    )
    monkeypatch.setattr("dgov.plan_review.load_run_envelope", lambda *_args, **_kwargs: envelope)


# -- Staleness --


def test_list_stale_compiled_plan(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = _make_plan_tree_under_plans(plans, "stale-plan")
    monkeypatch.chdir(root)
    _compile_plan(runner, plan_dir)

    time.sleep(0.1)
    source = plan_dir / "core" / "work.toml"
    source.write_text(source.read_text() + "\n# touched\n")

    result = runner.invoke(cli, ["plan", "list"])
    assert result.exit_code == 0, result.output
    assert "stale" in result.output.lower()
    assert "complete" not in result.output.lower()


def test_list_stale_compiled_plan_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = _make_plan_tree_under_plans(plans, "stale-plan")
    monkeypatch.chdir(root)
    _compile_plan(runner, plan_dir)

    time.sleep(0.1)
    source = plan_dir / "core" / "work.toml"
    source.write_text(source.read_text() + "\n# touched\n")

    result = runner.invoke(cli, ["--json", "plan", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["status"] == "stale"
    assert entry["stale"] is True


# -- Degraded / remediation --


def test_list_degraded_fully_deployed(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.deploy_log import append as deploy_append
    from dgov.plan_review import RunEnvelope

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = _make_plan_tree_under_plans(plans, "degraded-plan")
    monkeypatch.chdir(root)
    _patched_run_envelope(
        monkeypatch,
        envelope=RunEnvelope(
            plan_name="degraded-plan",
            last_run_ts="2026-04-10T12:00:00Z",
            run_status="degraded",
            sentrux_degradation=True,
            sentrux_offender_summary="1 offender in src/module.py",
        ),
    )
    _compile_plan(runner, plan_dir)
    deploy_append(str(root), "degraded-plan", "core/work.alpha", "sha1", "2026-04-06T12:00:00Z")
    deploy_append(str(root), "degraded-plan", "core/work.beta", "sha2", "2026-04-06T12:00:00Z")

    result = runner.invoke(cli, ["plan", "list"])
    assert result.exit_code == 0, result.output
    assert "degraded" in result.output.lower()
    assert "complete" not in result.output.lower()


def test_list_empty_plan_not_marked_stale(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compiled plan with zero units stays 'empty' even when source is newer than compiled."""
    import time

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = plans / "empty-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text('[plan]\nname = "empty-plan"\nsections = []\n')
    (plan_dir / "_compiled.toml").write_text('[plan]\nname = "empty-plan"\n')
    monkeypatch.chdir(root)

    time.sleep(0.1)
    # Touch the source so its mtime is now newer than the compiled artifact.
    (plan_dir / "_root.toml").write_text('[plan]\nname = "empty-plan"\nsections = []\n# bump\n')

    result = runner.invoke(cli, ["plan", "list"])
    assert result.exit_code == 0, result.output
    assert "empty" in result.output.lower()
    assert "stale" not in result.output.lower()


def test_list_empty_plan_not_marked_stale_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON entry for an empty compiled plan stays status=empty regardless of stale."""
    import time

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = plans / "empty-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text('[plan]\nname = "empty-plan"\nsections = []\n')
    (plan_dir / "_compiled.toml").write_text('[plan]\nname = "empty-plan"\n')
    monkeypatch.chdir(root)

    time.sleep(0.1)
    (plan_dir / "_root.toml").write_text('[plan]\nname = "empty-plan"\nsections = []\n# bump\n')

    result = runner.invoke(cli, ["--json", "plan", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["status"] == "empty"
    assert entry["total"] == 0


def test_list_degraded_fully_deployed_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.deploy_log import append as deploy_append
    from dgov.plan_review import RunEnvelope

    root = _make_project_root(tmp_path)
    plans = root / ".dgov" / "plans"
    plan_dir = _make_plan_tree_under_plans(plans, "degraded-plan")
    monkeypatch.chdir(root)
    _patched_run_envelope(
        monkeypatch,
        envelope=RunEnvelope(
            plan_name="degraded-plan",
            last_run_ts="2026-04-10T12:00:00Z",
            run_status="degraded",
            sentrux_degradation=True,
            sentrux_offender_summary="1 offender in src/module.py",
        ),
    )
    _compile_plan(runner, plan_dir)
    deploy_append(str(root), "degraded-plan", "core/work.alpha", "sha1", "2026-04-06T12:00:00Z")
    deploy_append(str(root), "degraded-plan", "core/work.beta", "sha2", "2026-04-06T12:00:00Z")

    result = runner.invoke(cli, ["--json", "plan", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["status"] == "degraded"
    assert entry["run_status"] == "degraded"
    assert entry["remediation_needed"] is True
