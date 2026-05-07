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


def _patched_run_envelope(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Stub plan_review.load_run_envelope so status tests can control run-level fields."""
    from dgov.plan_review import RunEnvelope

    envelope = overrides.get(
        "envelope",
        RunEnvelope(plan_name="test-plan", last_run_ts=None),
    )
    monkeypatch.setattr("dgov.plan_review.load_run_envelope", lambda *_args, **_kwargs: envelope)


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


def test_status_missing_plan_reports_error(runner: CliRunner, tmp_path: Path) -> None:
    ghost = tmp_path / ".dgov" / "plans" / "ghost"
    result = runner.invoke(cli, ["plan", "status", str(ghost)])
    assert result.exit_code != 0
    assert "plan path not found" in result.output


# -- All pending --


def test_status_all_pending(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        # Need cwd to be the project root for deploy log lookup
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir), "--verbose"])
    assert result.exit_code == 0
    assert "0/2 deployed" in result.output
    assert "2 pending" in result.output
    assert "○" in result.output


def test_status_default_is_one_line_summary(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default output is a single-line summary; per-unit list is --verbose only."""
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "0/2 deployed" in result.output
    # Per-unit markers should not appear by default
    assert "○" not in result.output
    assert "blocked by" not in result.output


def test_status_all_pending_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
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


def test_status_partial_deploy(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "abc1234", "2026-04-06T12:00:00Z")
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "1/2 deployed" in result.output
    assert "1 pending" in result.output


def test_status_partial_deploy_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch)
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


def test_status_blocked_by_shown(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir), "--verbose"])
    assert result.exit_code == 0
    # beta depends on alpha; neither deployed → beta blocked by alpha
    assert "blocked by" in result.output


def test_status_blocked_by_cleared_after_deploy(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    beta = next(u for u in data["unit_statuses"] if u["unit"] == "core/work.beta")
    # alpha is deployed, so beta should not be blocked
    assert beta["blocked_by"] == ""


def test_status_after_recompiled_dag_growth(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Growing and recompiling a previously deployed plan updates status immediately."""
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")

        (plan_dir / "core" / "extra.toml").write_text(
            "[tasks.gamma]\n"
            'summary = "Gamma"\nprompt = "Do gamma"\ncommit_message = "gamma"\n'
            'depends_on = ["core/work.beta"]\nfiles.create = ["c.py"]\n'
        )
        _compile_plan(runner, plan_dir)

        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["units"] == 3
    assert data["deployed"] == 1
    assert data["pending"] == 2
    assert data["stale"] is False

    statuses = {u["unit"]: u for u in data["unit_statuses"]}
    assert statuses["core/work.alpha"]["status"] == "deployed"
    assert statuses["core/work.beta"]["status"] == "pending"
    assert statuses["core/extra.gamma"]["blocked_by"] == "core/work.beta"


# -- Staleness --


def test_status_stale_detection(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        # Touch a source file to make it newer than the compiled plan
        time.sleep(0.1)
        source = plan_dir / "core" / "work.toml"
        source.write_text(source.read_text() + "\n# touched\n")
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    assert "stale" in result.output.lower()


def test_status_stale_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        time.sleep(0.1)
        source = plan_dir / "core" / "work.toml"
        source.write_text(source.read_text() + "\n# touched\n")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stale"] is True


def test_status_stale_when_root_changes(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        time.sleep(0.1)
        root_file = plan_dir / "_root.toml"
        root_file.write_text(root_file.read_text() + "\n# touched\n")
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stale"] is True


def test_status_uses_compiled_source_metadata_for_staleness(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    plan_dir = _make_plan_tree(tmp_path)
    compiled_path = plan_dir / "_compiled.toml"
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        time.sleep(0.1)
        source = plan_dir / "core" / "work.toml"
        source.write_text(source.read_text() + "\n# touched\n")
        time.sleep(0.1)
        compiled_path.touch()
        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stale"] is True


def test_status_resolves_archived_plan_and_emits_note(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _patched_run_envelope(monkeypatch)
        _compile_plan(runner, plan_dir)
        archive_root = plan_dir.parent / "archive"
        archive_root.mkdir()
        archived_dir = archive_root / plan_dir.name
        plan_dir.rename(archived_dir)

        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "resolved to archived plan" in result.output
    assert str(archived_dir) in result.output


def test_status_shows_degraded_follow_up_hint(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    envelope = RunEnvelope(
        plan_name="test-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
        sentrux_offender_summary="2 offenders in src/module.py",
    )
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch, envelope=envelope)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")
        deploy_append(td, "test-plan", "core/work.beta", "sha2")

        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "run status: degraded" in result.output
    assert "deployed but unresolved" in result.output
    assert f"dgov plan remediate {plan_dir}" in result.output


def test_status_uses_runs_log_fallback_for_degraded_hint(
    runner: CliRunner, tmp_path: Path
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")
        deploy_append(td, "test-plan", "core/work.beta", "sha2")
        runs_log = Path(td) / ".dgov" / "runs.log"
        runs_log.parent.mkdir(parents=True, exist_ok=True)
        runs_log.write_text(
            "[2026-01-01 00:00:00Z] test-plan (.dgov/plans/test-plan) — warn (10.5s)\n"
            "  sentrux: 95 -> 85\n"
            "  sentrux_status: degradation\n"
            "  sentrux_offenders: 1 offender in src/module.py\n"
        )

        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "run status: degraded" in result.output
    assert "deployed but unresolved" in result.output


def test_status_json_includes_remediation_fields(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    envelope = RunEnvelope(
        plan_name="test-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
        sentrux_offender_summary="1 offender in src/module.py",
    )
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch, envelope=envelope)
        _compile_plan(runner, plan_dir)
        deploy_append(td, "test-plan", "core/work.alpha", "sha1")
        deploy_append(td, "test-plan", "core/work.beta", "sha2")

        result = runner.invoke(cli, ["--json", "plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["run_status"] == "degraded"
    assert data["sentrux_degradation"] is True
    assert data["sentrux_offender_summary"] == "1 offender in src/module.py"
    assert data["remediation_needed"] is True
    assert data["next_action"] == f"dgov plan remediate {plan_dir}"


def test_status_shows_branch_verification_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    envelope = RunEnvelope(
        plan_name="test-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        branch_verification_status="failed",
        branch_verification_error="Type check failure",
    )
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        plan_dir = _make_plan_tree(Path(td))
        _patched_run_envelope(monkeypatch, envelope=envelope)
        _compile_plan(runner, plan_dir)

        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "branch status: failed" in result.output
    assert "Type check failure" in result.output


def test_status_does_not_call_full_review_loader(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    monkeypatch.setattr(
        "dgov.plan_review.load_review",
        lambda **_: (_ for _ in ()).throw(AssertionError("load_review should not be used")),
    )
    _patched_run_envelope(monkeypatch)

    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        os.chdir(td)
        _compile_plan(runner, plan_dir)
        result = runner.invoke(cli, ["plan", "status", str(plan_dir)])

    assert result.exit_code == 0, result.output


# -- help --


def test_plan_status_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["plan", "status", "--help"])
    assert result.exit_code == 0
    assert "deployment status" in result.output.lower()
