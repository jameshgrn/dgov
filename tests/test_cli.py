"""Tests for dgov CLI commands."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import compile_plan_tree
from rich.console import Console

from dgov.bootstrap_policy import BOOTSTRAP_SOP_FILENAMES
from dgov.cli import cli
from dgov.cli.init import (
    _detect_project,
    _detect_scope_ignore_files,
    _python_project_uses_pytest,
    _render_governor_md,
    _render_project_toml,
)
from dgov.cli.watch import (
    _default_watch_state,
    _format_event,
    _infer_plan_name_from_active_tasks,
)
from dgov.event_types import (
    IntegrationRiskScored,
    SemanticGateRejected,
    SettlementRetry,
    TaskDone,
    WorkerLog,
)
from dgov.persistence import (
    emit_event,
    list_runtime_artifacts,
    record_runtime_artifact,
)
from dgov.persistence.schema import WorkerTask
from dgov.types import TaskState

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_json_env():
    """Prevent DGOV_JSON from leaking between tests."""
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


def _make_compiled_plan(
    project_root: Path, plan_name: str, unit_summaries: dict[str, str]
) -> Path:
    """Write a minimal _compiled.toml into a plan directory under project_root."""
    plan_dir = project_root / ".dgov" / "plans" / plan_name
    plan_dir.mkdir(parents=True)
    lines = [
        "[plan]",
        f'name = "{plan_name}"',
        'source_mtime_max = "2026-04-10T12:00:00.000000+00:00"',
        "",
    ]
    for uid, summary in unit_summaries.items():
        lines.append(f'[tasks."{uid}"]')
        lines.append(f'summary = "{summary}"')
        lines.append('prompt = "do it"')
        lines.append('commit_message = "c"')
        lines.append('files.create = ["a.py"]')
        lines.append("")
    compiled_path = plan_dir / "_compiled.toml"
    compiled_path.write_text("\n".join(lines))
    (plan_dir / "_root.toml").write_text(
        f'[plan]\nname = "{plan_name}"\nsummary = "t"\nsections = ["tasks"]\n'
    )
    return plan_dir


def _patched_load_review(monkeypatch, **overrides):
    """Return a helper that stubs load_review to return a fixed PlanReview."""
    from dgov.plan_review import DiffStat, PlanReview, UnitReview

    default_unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        agent="kimi",
        commit_sha="abcd1234" + "0" * 32,
        commit_message="feat: did a",
        commit_ts="2026-04-10T12:00:00Z",
        diff_stat=DiffStat(files_changed=1, insertions=10, deletions=0),
        landed_files=("src/dgov/example.py",),
        duration_s=12.5,
        iterations=4,
        attempts=1,
        settlement="ok",
        done_summary="Added the thing.",
    )
    default_review = PlanReview(
        plan_name="p",
        source_dir=Path("p"),
        last_run_ts="2026-04-10T12:00:00Z",
        last_run_duration_s=12.5,
        units=[default_unit],
    )
    review = overrides.get("review", default_review)

    def _fake(**kwargs):
        # Honor only-filter semantics for the `only=...` tests.
        only = kwargs.get("only")
        if only is not None:
            filtered = [u for u in review.units if u.unit == only]
            return PlanReview(
                plan_name=review.plan_name,
                source_dir=review.source_dir,
                last_run_ts=review.last_run_ts,
                last_run_duration_s=review.last_run_duration_s,
                units=filtered,
            )
        return review

    monkeypatch.setattr("dgov.plan_review.load_review", _fake)


def _json_integration_review():
    from dgov.plan_review import DiffStat, PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        commit_sha="abc1234",
        commit_message="feat: a",
        diff_stat=DiffStat(files_changed=1, insertions=2, deletions=0),
        landed_files=("src/a.py",),
        settlement="ok",
        integration_risk_level="medium",
        integration_risk_detected=True,
        integration_candidate_passed=True,
        integration_failure_class=None,
        integration_claimed_files=("src/a.py",),
        integration_changed_files=("src/a.py",),
        integration_gate_name=None,
        integration_error=None,
        integration_evidence=("same-symbol edit: function foo in src/a.py",),
    )
    return PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )


def _patched_run_envelope(monkeypatch, **overrides):
    """Stub load_run_envelope to return a fixed run-level snapshot."""
    from dgov.plan_review import RunEnvelope

    envelope = overrides.get("envelope", RunEnvelope(plan_name="p", last_run_ts=None))
    monkeypatch.setattr("dgov.plan_review.load_run_envelope", lambda *_args, **_kwargs: envelope)


def _emit_active_and_settling_sequence(project_root: Path, plan_name: str) -> None:
    root = str(project_root)
    emit_event(root, "run_start", "run-plan", plan_name=plan_name)
    task_events = (
        ("dag_task_dispatched", "pane-active", "active-task", {}),
        ("dag_task_dispatched", "pane-settling", "settling-task", {}),
        ("task_done", "pane-settling", "settling-task", {}),
        ("review_pass", "pane-settling", "settling-task", {}),
        ("settlement_phase_started", "pane-settling", "settling-task", {"phase": "integration"}),
    )
    for event, pane, task_slug, extra in task_events:
        emit_event(root, event, pane, plan_name=plan_name, task_slug=task_slug, **extra)


def _assert_settling_task_has_phase(data: dict, expected_phase: str) -> None:
    """Assert that the settling task in JSON status has the expected phase."""
    task_list = data.get("task_list", [])
    settling_tasks = [t for t in task_list if t.get("slug") == "settling-task"]
    assert len(settling_tasks) == 1
    assert settling_tasks[0].get("phase") == expected_phase


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


def test_status_from_inside_dgov_uses_repo_root(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    monkeypatch.chdir(dgov_dir)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert (tmp_path / ".dgov" / "state.db").exists()
    assert not (tmp_path / ".dgov" / ".dgov" / "state.db").exists()


def test_status_json(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["--json", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "status" in data
    assert "tasks" in data


def test_status_hides_history_by_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )
    emit_event(
        str(tmp_path), "task_done", "pane-merged", plan_name="plan-a", task_slug="merged-task"
    )
    emit_event(
        str(tmp_path),
        "review_pass",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )
    emit_event(
        str(tmp_path),
        "merge_completed",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )
    emit_event(str(tmp_path), "run_start", "run-plan-b", plan_name="plan-b")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-active",
        plan_name="plan-b",
        task_slug="active-task",
    )

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "active-task" in result.output
    assert "merged-task" not in result.output


def test_status_all_shows_history(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )
    emit_event(
        str(tmp_path), "task_done", "pane-merged", plan_name="plan-a", task_slug="merged-task"
    )
    emit_event(
        str(tmp_path),
        "review_pass",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )
    emit_event(
        str(tmp_path),
        "merge_completed",
        "pane-merged",
        plan_name="plan-a",
        task_slug="merged-task",
    )

    result = runner.invoke(cli, ["status", "--all"])
    assert result.exit_code == 0
    assert "merged-task" in result.output


def test_status_scopes_live_view_to_latest_run_start(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-stale",
        plan_name="plan-a",
        task_slug="stale-task",
    )
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "status: idle" in result.output
    assert "stale-task" not in result.output


def test_status_treats_run_completed_as_terminal(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-stale",
        plan_name="plan-a",
        task_slug="stale-task",
    )
    emit_event(
        str(tmp_path),
        "run_completed",
        "run-plan",
        plan_name="plan-a",
        run_status="degraded",
    )

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "status: idle" in result.output
    assert "active: 0" in result.output
    assert "stale-task" not in result.output


def test_status_all_marks_unterminated_completed_run_stale(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-stale",
        plan_name="plan-a",
        task_slug="stale-task",
    )
    emit_event(
        str(tmp_path),
        "run_completed",
        "run-plan",
        plan_name="plan-a",
        run_status="degraded",
    )

    result = runner.invoke(cli, ["status", "--all"])

    assert result.exit_code == 0
    assert "status: idle" in result.output
    assert "active: 0" in result.output
    assert "stale-task" in result.output
    assert "stale" in result.output
    assert "active          stale-task" not in result.output


def test_status_shows_reviewed_failure_as_live(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )
    emit_event(
        str(tmp_path),
        "task_done",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )
    emit_event(
        str(tmp_path),
        "review_fail",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    # reviewed_fail is a live state — task awaits governor handling, not terminal.
    assert "status: needs_attention" in result.output
    assert "failed-review-task" in result.output
    assert "reviewed_fail" in result.output


def test_status_all_shows_reviewed_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )
    emit_event(
        str(tmp_path),
        "task_done",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )
    emit_event(
        str(tmp_path),
        "review_fail",
        "pane-failed-review",
        plan_name="plan-a",
        task_slug="failed-review-task",
    )

    result = runner.invoke(cli, ["status", "--all"])

    assert result.exit_code == 0
    assert "status: needs_attention" in result.output
    assert "reviewed_fail" in result.output
    assert "failed-review-task" in result.output


def test_status_shows_settling_task_after_review_pass(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task in settlement phase should remain visible as settling after review_pass."""
    monkeypatch.chdir(tmp_path)
    emit_event(str(tmp_path), "run_start", "run-plan", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-settling",
        plan_name="plan-a",
        task_slug="settling-task",
    )
    emit_event(
        str(tmp_path),
        "task_done",
        "pane-settling",
        plan_name="plan-a",
        task_slug="settling-task",
    )
    emit_event(
        str(tmp_path),
        "review_pass",
        "pane-settling",
        plan_name="plan-a",
        task_slug="settling-task",
    )
    emit_event(
        str(tmp_path),
        "settlement_phase_started",
        "pane-settling",
        plan_name="plan-a",
        task_slug="settling-task",
        phase="integration",
    )

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "status: active" in result.output
    assert "settling-task" in result.output
    assert "settling" in result.output


def test_status_json_includes_phase_and_state_counts(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON status output should include phase and state_counts."""
    monkeypatch.chdir(tmp_path)
    _emit_active_and_settling_sequence(tmp_path, plan_name="plan-a")

    result = runner.invoke(cli, ["--json", "status"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "state_counts" in data
    assert "settling" in data.get("state_counts", {})
    assert data["settling"] == 1
    assert "attention" in data
    assert data["attention"] == 0
    _assert_settling_task_has_phase(data, expected_phase="integration")


# -- validate --


def test_validate_valid_plan(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        tasks_toml = """
[tasks.a]
summary = "do a"
prompt = "do a"
commit_message = "a"
files = ["a.py"]
"""
        compiled_path = compile_plan_tree(tmp_path, "test-valid", tasks_toml)

        result = runner.invoke(cli, ["validate", str(compiled_path)])
        assert result.exit_code == 0
        assert "Validation passed" in result.output
        assert "do a" in result.output


def test_validate_conflict_plan(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        plan_dir = Path(".dgov") / "plans" / "test-conflict"
        plan_dir.mkdir(parents=True)
        compiled_path = plan_dir / "_compiled.toml"
        compiled_path.write_text(
            '[plan]\nname = "test-conflict"\nsource_mtime_max = "2026-04-10T12:00:00Z"\n\n'
            "[tasks.a]\n"
            'summary = "a"\n'
            'prompt = "a"\n'
            'commit_message = "a"\n'
            'files.edit = ["shared.py"]\n\n'
            "[tasks.b]\n"
            'summary = "b"\n'
            'prompt = "b"\n'
            'commit_message = "b"\n'
            'files.edit = ["shared.py"]\n',
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["validate", str(compiled_path)])
        assert result.exit_code != 0
        assert "ERROR" in result.output
        assert "File conflict" in result.output


def test_validate_json_output(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        tasks_toml = """
[tasks.a]
summary = "a"
prompt = "a"
commit_message = "a"
"""
        compiled_path = compile_plan_tree(tmp_path, "test-json", tasks_toml)

        result = runner.invoke(cli, ["--json", "validate", str(compiled_path)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["valid"] is True
        assert data["tasks"] == 1


def test_validate_rejects_unknown_task_provider(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        dgov_dir = root / ".dgov"
        plan_dir = dgov_dir / "plans" / "provider"
        plan_dir.mkdir(parents=True)
        (dgov_dir / "project.toml").write_text(
            """
[project]
provider = "test-provider"

[providers.test-provider]
default_agent = "provider/model-name"
base_url = "https://provider.example.com/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
""",
            encoding="utf-8",
        )
        compiled_path = plan_dir / "_compiled.toml"
        compiled_path.write_text(
            '[plan]\nname = "provider"\nsource_mtime_max = "2026-04-10T12:00:00Z"\n\n'
            "[tasks.a]\n"
            'summary = "Do a"\n'
            'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
            'commit_message = "a"\n'
            'provider = "missing"\n'
            'agent = "some/model"\n'
            'files = ["README.md"]\n',
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["validate", str(compiled_path)])

        assert result.exit_code != 0
        assert "Unknown provider 'missing'" in result.output


def test_validate_rejects_missing_provider_config(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        dgov_dir = root / ".dgov"
        plan_dir = dgov_dir / "plans" / "provider"
        plan_dir.mkdir(parents=True)
        (dgov_dir / "project.toml").write_text("[project]\n", encoding="utf-8")
        compiled_path = plan_dir / "_compiled.toml"
        compiled_path.write_text(
            '[plan]\nname = "provider"\nsource_mtime_max = "2026-04-10T12:00:00Z"\n\n'
            "[tasks.a]\n"
            'summary = "Do a"\n'
            'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
            'commit_message = "a"\n'
            'agent = "some/model"\n'
            'files = ["README.md"]\n',
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["validate", str(compiled_path)])

        assert result.exit_code != 0
        assert "No provider for task" in result.output


def test_validate_rejects_department_violation(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        dgov_dir = root / ".dgov"
        plan_dir = dgov_dir / "plans" / "constitution"
        plan_dir.mkdir(parents=True)
        (dgov_dir / "project.toml").write_text(
            '[departments]\nCore = ["src/dgov/kernel.py"]\n',
            encoding="utf-8",
        )
        compiled_path = plan_dir / "_compiled.toml"
        compiled_path.write_text(
            '[plan]\nname = "constitution"\nsource_mtime_max = "2026-04-10T12:00:00Z"\n\n'
            "[tasks.a]\n"
            'summary = "Do a"\n'
            'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
            'commit_message = "a"\n'
            'files = ["src/dgov/kernel.py"]\n',
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["validate", str(compiled_path)])

        assert result.exit_code != 0
        assert "Constitutional violation" in result.output


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


def test_init_creates_bootstrap_files(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: False)
        Path(td, "src").mkdir()
        Path(td, "tests").mkdir()
        Path(td, "main.py").touch()
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "Created" in result.output
        config = Path(td, ".dgov", "project.toml")
        governor = Path(td, ".dgov", "governor.md")
        sops_dir = Path(td, ".dgov", "sops")
        assert config.exists()
        assert governor.exists()
        assert sops_dir.is_dir()
        assert "plans/archive/" not in Path(td, ".dgov", ".gitignore").read_text()
        content = config.read_text()
        assert 'language = "python"' in content
        assert 'src_dir = "src/"' in content
        assert '# provider = "your-provider"' in content
        assert "# [providers.your-provider]" in content
        assert '# default_agent = "provider/model-name"' in content
        assert '# base_url = "https://provider.example.com/v1"' in content
        assert '# api_key_env = "YOUR_PROVIDER_API_KEY"' in content
        assert '# Run "dgov sentrux gate-save" after bootstrap' in content
        assert 'test_cmd = "uv run pytest {test_dir} -q --tb=short"' in content
        assert 'lint_cmd = "uv run ruff check {file}"' in content
        assert "# Governor Charter" in governor.read_text()
        assert "## Plan Authoring Workflow" in governor.read_text()
        assert "## Done Criteria" in governor.read_text()
        for name in BOOTSTRAP_SOP_FILENAMES:
            assert (sops_dir / name).exists()
        assert "Next:" in result.output
        assert ".dgov/sops/" in result.output
        assert "dgov sentrux gate-save" in result.output


def test_sentrux_check_passes_requested_path_without_chdir(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)

    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(
            ["sentrux", *args], 0, stdout="Quality: 42\n", stderr=""
        )

    monkeypatch.setattr("dgov.cli.sentrux.run_sentrux", _mock_run)

    result = runner.invoke(cli, ["sentrux", "check", "src"])

    assert result.exit_code == 0
    assert calls == [(["check", "src"], None)]


def test_sentrux_check_reports_stdout_and_stderr_on_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)

    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        raise subprocess.CalledProcessError(
            1,
            ["sentrux", *args],
            output="Quality: 6656\nmin_quality failed\n",
            stderr="Scanning ...\n",
        )

    monkeypatch.setattr("dgov.cli.sentrux.run_sentrux", _mock_run)

    result = runner.invoke(cli, ["sentrux", "check"])

    assert result.exit_code == 1
    assert "Quality: 6656" in result.output
    assert "min_quality failed" in result.output
    assert "Scanning ..." in result.output


def _init_cli_git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_cli_sentrux_baseline(tmp_path: Path) -> str:
    sentrux_dir = tmp_path / ".sentrux"
    sentrux_dir.mkdir()
    (sentrux_dir / "baseline.json").write_text('{"quality": 1}\n')
    subprocess.run(["git", "add", ".sentrux/baseline.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "save baseline"], cwd=tmp_path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _mock_sentrux_gate_save(
    tmp_path: Path,
    *,
    assert_canonical_target: bool,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        if assert_canonical_target:
            assert args == ["gate", "--save", str(tmp_path.resolve())]
        sentrux_dir = tmp_path / ".sentrux"
        sentrux_dir.mkdir(exist_ok=True)
        (sentrux_dir / "baseline.json").write_text('{"quality": 42}\n')
        return subprocess.CompletedProcess(
            ["sentrux", *args], 0, stdout="Quality: 42\n", stderr=""
        )

    return _mock_run


def test_sentrux_gate_save_records_dgov_metadata(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_cli_git_repo(tmp_path)
    head = _commit_cli_sentrux_baseline(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)
    monkeypatch.setattr(
        "dgov.cli.sentrux.run_sentrux",
        _mock_sentrux_gate_save(tmp_path, assert_canonical_target=True),
    )

    result = runner.invoke(cli, ["sentrux", "gate-save"], catch_exceptions=False)

    metadata = json.loads((tmp_path / ".sentrux" / "dgov-baseline.json").read_text())
    assert result.exit_code == 0
    assert "Baseline metadata saved" in result.output
    assert metadata["accepted_head"] == head
    assert metadata["quality"] == 42


def test_sentrux_gate_save_skips_metadata_for_dirty_source_tree(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_cli_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("dirty\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)
    monkeypatch.setattr(
        "dgov.cli.sentrux.run_sentrux",
        _mock_sentrux_gate_save(tmp_path, assert_canonical_target=False),
    )

    result = runner.invoke(cli, ["sentrux", "gate-save"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Baseline metadata not recorded" in result.output
    assert not (tmp_path / ".sentrux" / "dgov-baseline.json").exists()


def test_sentrux_gate_fail_on_degradation_uses_command_output(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)

    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        return subprocess.CompletedProcess(
            ["sentrux", *args],
            1,
            stdout="✗ Degradation detected\n",
            stderr="",
        )

    monkeypatch.setattr("dgov.cli.sentrux.run_sentrux", _mock_run)

    result = runner.invoke(cli, ["sentrux", "gate", "--fail-on-degradation"])

    assert result.exit_code == 1
    assert "Degradation detected" in result.output


def test_sentrux_gate_prints_structural_offender_report_on_degradation(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)

    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        return subprocess.CompletedProcess(
            ["sentrux", *args],
            1,
            stdout="✗ Degradation detected\n",
            stderr="",
        )

    monkeypatch.setattr("dgov.cli.sentrux.run_sentrux", _mock_run)
    monkeypatch.setattr(
        "dgov.cli.sentrux._structural_offender_report",
        lambda target: "Likely structural offenders:\n- Complex functions:",
    )

    result = runner.invoke(cli, ["sentrux", "gate"])

    assert result.exit_code == 0
    assert "Likely structural offenders:" in result.output


def test_sentrux_offenders_reports_clean_empty_state(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dgov.cli.sentrux._structural_offender_report",
        lambda target: "No structural offenders found at abcdef123456.",
    )

    result = runner.invoke(cli, ["sentrux", "offenders"])

    assert result.exit_code == 0
    assert result.output == "No structural offenders found at abcdef123456.\n"


def test_sentrux_gate_treats_degraded_output_as_degradation(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dgov.cli.sentrux.sentrux_available", lambda: True)

    def _mock_run(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ):
        return subprocess.CompletedProcess(
            ["sentrux", *args],
            1,
            stdout="✗ DEGRADED\n",
            stderr="",
        )

    monkeypatch.setattr("dgov.cli.sentrux.run_sentrux", _mock_run)
    monkeypatch.setattr("dgov.cli.sentrux._structural_offender_report", lambda target: None)

    result = runner.invoke(cli, ["sentrux", "gate", "--fail-on-degradation"])

    assert result.exit_code == 1
    assert "Degradation detected" in result.output


def test_preflight_command_reports_pass(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.settlement import GateResult

    monkeypatch.setattr("dgov.cli.preflight.resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr("dgov.cli.preflight.find_policy_drift", lambda project_root: [])
    monkeypatch.setattr(
        "dgov.cli.preflight.preflight_sandbox",
        lambda worktree_path, project_root: GateResult(passed=True),
    )

    result = runner.invoke(cli, ["preflight"])

    assert result.exit_code == 0
    assert "Preflight passed." in result.output


def test_preflight_command_reports_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.settlement import GateResult

    monkeypatch.setattr("dgov.cli.preflight.resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr("dgov.cli.preflight.find_policy_drift", lambda project_root: [])
    monkeypatch.setattr(
        "dgov.cli.preflight.preflight_sandbox",
        lambda worktree_path, project_root: GateResult(passed=False, error="Lint failure:\nboom"),
    )

    result = runner.invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert "Preflight failed:" in result.output
    assert "Lint failure" in result.output


def test_preflight_command_reports_policy_source_drift(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.settlement import GateResult

    monkeypatch.setattr("dgov.cli.preflight.resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "dgov.cli.preflight.find_policy_drift",
        lambda project_root: ["AGENTS.md differs from CLAUDE.md"],
    )
    monkeypatch.setattr(
        "dgov.cli.preflight.preflight_sandbox",
        lambda worktree_path, project_root: GateResult(passed=True),
    )

    result = runner.invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert "Policy source drift:" in result.output
    assert "AGENTS.md differs from CLAUDE.md" in result.output


def test_init_refuses_overwrite(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        dgov_dir = Path(td, ".dgov")
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("[project]\n")
        (dgov_dir / "governor.md").write_text("# existing\n")
        sops_dir = dgov_dir / "sops"
        sops_dir.mkdir()
        for name in BOOTSTRAP_SOP_FILENAMES:
            (sops_dir / name).write_text(f"# {name}\n")
        result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0
        assert "Already initialized" in result.output


def test_init_creates_missing_governor_without_overwriting_project(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: False)
        dgov_dir = Path(td, ".dgov")
        dgov_dir.mkdir()
        existing = '[project]\nlanguage = "rust"\n'
        (dgov_dir / "project.toml").write_text(existing)
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "governor.md" in result.output
        assert (dgov_dir / "project.toml").read_text() == existing
        assert (dgov_dir / "governor.md").exists()
        for name in BOOTSTRAP_SOP_FILENAMES:
            assert (dgov_dir / "sops" / name).exists()


def test_init_preserves_existing_bootstrap_policy_files_without_force(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: False)
        dgov_dir = Path(td, ".dgov")
        sops_dir = dgov_dir / "sops"
        sops_dir.mkdir(parents=True)
        (dgov_dir / "project.toml").write_text('[project]\nlanguage = "rust"\n')
        (dgov_dir / "governor.md").write_text("# custom governor\n")
        (sops_dir / "testing.md").write_text("# custom testing\n")

        result = runner.invoke(cli, ["init"])

        assert result.exit_code == 0
        assert (dgov_dir / "project.toml").read_text() == '[project]\nlanguage = "rust"\n'
        assert (dgov_dir / "governor.md").read_text() == "# custom governor\n"
        assert (sops_dir / "testing.md").read_text() == "# custom testing\n"
        for name in BOOTSTRAP_SOP_FILENAMES:
            assert (sops_dir / name).exists()


def test_init_force_overwrites(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: False)
        dgov_dir = Path(td, ".dgov")
        sops_dir = dgov_dir / "sops"
        sops_dir.mkdir(parents=True)
        (dgov_dir / "project.toml").write_text("[project]\n")
        (dgov_dir / "governor.md").write_text("# old\n")
        (sops_dir / "testing.md").write_text("# old testing\n")
        result = runner.invoke(cli, ["init", "--force"])
        assert result.exit_code == 0
        assert "project.toml" in result.output
        assert "governor.md" in result.output
        assert "# Governor Charter" in (dgov_dir / "governor.md").read_text()
        assert "Testing Conventions" in (sops_dir / "testing.md").read_text()
        for name in BOOTSTRAP_SOP_FILENAMES:
            assert (sops_dir / name).exists()


def test_init_auto_creates_sentrux_baseline_in_headless(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # CliRunner.stdin.isatty() is False by default
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: True)
        called = False

        def _mock_save(root: Path) -> tuple[bool, str]:
            nonlocal called
            called = True
            return True, "saved"

        monkeypatch.setattr("dgov.cli.init._save_sentrux_baseline", _mock_save)
        result = runner.invoke(cli, ["init"])

        assert result.exit_code == 0
        # Should NOT prompt
        assert "Run `dgov sentrux gate-save` now to create the repo baseline?" not in result.output
        # Should auto-create
        assert called is True
        assert "Created" in result.output
        assert "baseline.json" in result.output


def test_init_offers_and_creates_sentrux_baseline(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: True)
        monkeypatch.setattr("dgov.cli.init._save_sentrux_baseline", lambda root: (True, "saved"))
        # Force automation with --yes
        result = runner.invoke(cli, ["init", "--yes"])

        assert result.exit_code == 0
        assert "Created" in result.output
        assert ".sentrux/baseline.json" in result.output


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
    lang, _src, _test, ext = _detect_project(tmp_path)
    assert lang == "rust"
    assert ".rs" in ext


def test_detect_fallback_to_unknown(tmp_path: Path) -> None:
    """Empty dirs are low-confidence unknown projects, not Python by assumption."""
    lang, _src, _test, _ext = _detect_project(tmp_path)
    assert lang == "unknown"


def test_init_project_type_swift_writes_swift_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        monkeypatch.setattr("dgov.cli.init._sentrux_available", lambda: False)

        result = runner.invoke(cli, ["init", "--project-type", "swift", "--preflight"])

        assert result.exit_code == 0, result.output
        config = Path(td, ".dgov", "project.toml").read_text()
        assert 'language = "swift"' in config
        assert 'src_dir = "Sources/"' in config
        assert 'test_dir = "Tests/"' in config
        assert 'lint_cmd = ""  # Configure a project-local lint command' in config
        assert '# setup_cmd = ""  # Runs in worktree before gates' in config
        assert "xcodegen" not in config
        assert "xcrun swift-format" not in config
        assert "worker env:" in result.output


# -- _render_project_toml --


def test_render_project_toml() -> None:
    content = _render_project_toml("python", "src/", "tests/", [".py"], ["uv.lock"])
    assert "[project]" in content
    assert 'language = "python"' in content
    assert '# provider = "your-provider"' in content
    assert "# [providers.your-provider]" in content
    assert '# api_key_env = "YOUR_PROVIDER_API_KEY"' in content
    assert 'format_cmd = "uv run ruff format {file}"' in content
    assert 'ignore_files = ["uv.lock"]' in content
    assert "built-in" in content
    assert "bootstrap_timeout = 300" in content
    assert "[conventions]" in content


def test_python_project_uses_pytest_from_dependency_group(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[dependency-groups]
dev = ["pytest[asyncio]>=8; python_version >= '3.11'", "ruff"]
""",
        encoding="utf-8",
    )

    assert _python_project_uses_pytest(tmp_path) is True


def test_python_project_uses_pytest_from_optional_dependency(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project.optional-dependencies]
test = ["pytest>=8", "pytest-cov"]
""",
        encoding="utf-8",
    )

    assert _python_project_uses_pytest(tmp_path) is True


def test_python_project_uses_pytest_from_requirements_file(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text(
        """
# test runner
pytest >= 8
""",
        encoding="utf-8",
    )

    assert _python_project_uses_pytest(tmp_path) is True


def test_python_project_uses_pytest_ignores_invalid_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("not valid toml {{{{", encoding="utf-8")

    assert _python_project_uses_pytest(tmp_path) is False


def test_detect_scope_ignore_files_adds_uv_lock_for_python(tmp_path: Path) -> None:
    assert _detect_scope_ignore_files(tmp_path, "python") == ["uv.lock"]


def test_render_governor_md() -> None:
    content = _render_governor_md()
    assert content.startswith("# Governor Charter")
    assert "## Plan Authoring Workflow" in content
    assert "## Done Criteria" in content
    assert ".dgov/sops/" in content


# -- help / version --


def test_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "dgov" in result.output
    assert "status" in result.output
    assert "validate" in result.output
    assert "init" in result.output
    assert "retry" not in result.output
    assert "mark-done" not in result.output
    assert "recover" not in result.output


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "dgov" in result.output
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] in result.output


# -- watch --


def test_watch_subcommand_registered(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["watch", "--help"])
    assert result.exit_code == 0
    assert "Stream" in result.output


def test_watch_help_shows_flags(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["watch", "--help"])
    assert result.exit_code == 0
    assert "--all" in result.output
    assert "--plan" in result.output
    assert "--root" in result.output


def test_watch_root_forwards_resolved_project_root(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    nested = project_root / "nested" / "deeper"
    (project_root / ".dgov").mkdir(parents=True)
    nested.mkdir(parents=True)
    captured: dict[str, object] = {}

    def _capture(project_root: str, watch_all: bool = False, plan_name: str | None = None) -> None:
        captured["project_root"] = project_root
        captured["watch_all"] = watch_all
        captured["plan_name"] = plan_name

    monkeypatch.setattr("dgov.cli.watch._cmd_watch", _capture)

    result = runner.invoke(
        cli,
        ["watch", "--root", str(nested), "--plan", "constitution"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured == {
        "project_root": str(project_root),
        "watch_all": False,
        "plan_name": "constitution",
    }


def test_plan_remediate_scaffolds_follow_up_plan(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    envelope = RunEnvelope(
        plan_name="source-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
        sentrux_offender_summary="2 offenders in src/a.py",
    )
    _patched_run_envelope(monkeypatch, envelope=envelope)
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 0, result.output
    remediation_dir = tmp_path / ".dgov" / "plans" / "source-plan-remediation"
    assert remediation_dir.exists()
    assert (remediation_dir / "_root.toml").exists()
    assert (remediation_dir / "fix" / "_context.md").exists()
    example_path = remediation_dir / "fix" / "_example.toml"
    assert example_path.exists()
    assert "dgov plan review" in example_path.read_text()
    assert "2 offenders in src/a.py" in (remediation_dir / "fix" / "_context.md").read_text()


def test_plan_remediate_rejects_non_degraded_plan(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    envelope = RunEnvelope(
        plan_name="source-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="complete",
    )
    _patched_run_envelope(monkeypatch, envelope=envelope)
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 1
    assert "fully deployed plans whose last run status is degraded" in result.output


def test_plan_remediate_reports_invalid_compiled_toml(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    (plan_dir / "_compiled.toml").write_text("this is not valid toml {{{")

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 1
    assert "Invalid compiled plan" in result.output


def test_plan_remediate_fails_when_live_plan_exists(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    envelope = RunEnvelope(
        plan_name="source-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
        sentrux_offender_summary="1 offender",
    )
    _patched_run_envelope(monkeypatch, envelope=envelope)
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")
    existing = tmp_path / ".dgov" / "plans" / "source-plan-remediation"
    existing.mkdir(parents=True)
    (existing / "_root.toml").write_text('[plan]\nname = "source-plan-remediation"\n')

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 1
    assert "remediation plan already exists" in result.output


def test_plan_remediate_explicit_name_fails_when_archived_name_exists(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    envelope = RunEnvelope(
        plan_name="source-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
    )
    _patched_run_envelope(monkeypatch, envelope=envelope)
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")
    archived = tmp_path / ".dgov" / "plans" / "archive" / "custom-name"
    archived.mkdir(parents=True)
    (archived / "_root.toml").write_text('[plan]\nname = "custom-name"\n')

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir), "--name", "custom-name"])

    assert result.exit_code == 1
    assert "remediation plan already exists" in result.output


def test_plan_remediate_generated_name_skips_live_suffixed_collision(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import RunEnvelope

    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    envelope = RunEnvelope(
        plan_name="source-plan",
        last_run_ts="2026-04-10T12:00:00Z",
        run_status="degraded",
        sentrux_degradation=True,
    )
    _patched_run_envelope(monkeypatch, envelope=envelope)
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")
    archived = tmp_path / ".dgov" / "plans" / "archive" / "source-plan-remediation"
    archived.mkdir(parents=True)
    (archived / "_root.toml").write_text('[plan]\nname = "source-plan-remediation"\n')
    live_suffix = tmp_path / ".dgov" / "plans" / "source-plan-remediation-2"
    live_suffix.mkdir(parents=True)
    (live_suffix / "_root.toml").write_text('[plan]\nname = "source-plan-remediation-2"\n')

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "Created remediation plan 'source-plan-remediation-3'" in result.output


def test_plan_remediate_uses_runs_log_fallback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_compiled_plan(tmp_path, "source-plan", {"tasks/main.a": "do a"})
    monkeypatch.chdir(tmp_path)
    from dgov.deploy_log import append as deploy_append

    deploy_append(str(tmp_path), "source-plan", "tasks/main.a", "sha1")
    runs_log = tmp_path / ".dgov" / "runs.log"
    runs_log.parent.mkdir(parents=True, exist_ok=True)
    runs_log.write_text(
        "[2026-01-01 00:00:00Z] source-plan (.dgov/plans/source-plan) — warn (10.5s)\n"
        "  sentrux: 95 -> 85\n"
        "  sentrux_status: degradation\n"
    )

    result = runner.invoke(cli, ["plan", "remediate", str(plan_dir)])

    assert result.exit_code == 0, result.output
    remediation_dir = tmp_path / ".dgov" / "plans" / "source-plan-remediation"
    assert remediation_dir.exists()


def test_infer_plan_name_no_active_tasks(tmp_path: Path) -> None:
    assert _infer_plan_name_from_active_tasks(str(tmp_path)) is None


def test_infer_plan_name_single_plan(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="fix/a",
    )
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-b",
        plan_name="plan-a",
        task_slug="fix/b",
    )
    assert _infer_plan_name_from_active_tasks(str(tmp_path)) == "plan-a"


def test_infer_plan_name_multiple_plans(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="fix/a",
    )
    emit_event(str(tmp_path), "run_start", "run-plan-b", plan_name="plan-b")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-b",
        plan_name="plan-b",
        task_slug="fix/b",
    )
    assert _infer_plan_name_from_active_tasks(str(tmp_path)) is None


def test_infer_plan_name_empty_plan_names(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "dag_task_dispatched", "pane-a", task_slug="fix/a")
    emit_event(str(tmp_path), "dag_task_dispatched", "pane-b", plan_name="", task_slug="fix/b")
    assert _infer_plan_name_from_active_tasks(str(tmp_path)) is None


def test_infer_plan_name_mixed_states(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="fix/a",
    )
    emit_event(str(tmp_path), "run_start", "run-plan-b", plan_name="plan-b")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-b",
        plan_name="plan-b",
        task_slug="fix/b",
    )
    emit_event(str(tmp_path), "task_done", "pane-b", plan_name="plan-b", task_slug="fix/b")
    emit_event(str(tmp_path), "review_pass", "pane-b", plan_name="plan-b", task_slug="fix/b")
    emit_event(
        str(tmp_path),
        "merge_completed",
        "pane-b",
        plan_name="plan-b",
        task_slug="fix/b",
    )
    assert _infer_plan_name_from_active_tasks(str(tmp_path)) == "plan-a"


def test_default_watch_state_uses_inferred_plan_history(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="fix/a",
    )
    assert _default_watch_state(str(tmp_path), watch_all=False, plan_name=None) == ("plan-a", 0)


def test_default_watch_state_tails_from_latest_event_without_plan(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "task_done", "pane-a", plan_name="old-plan")
    assert _default_watch_state(str(tmp_path), watch_all=False, plan_name=None) == (None, 1)


def test_format_event_shows_successful_verify_tool_results() -> None:
    renderable = _format_event(
        WorkerLog(
            task_slug="tasks/main.a",
            log_type="result",
            content={"status": "success", "tool": "run_tests"},
        ),
        "2026-04-24T12:34:56Z",
    )

    assert renderable is not None
    console = Console(record=True, width=120)
    console.print(renderable)
    rendered = console.export_text()
    assert "run_tests" in rendered
    assert "ok" in rendered


def test_format_event_renders_task_done_tokens() -> None:
    renderable = _format_event(
        TaskDone(task_slug="task-a", prompt_tokens=1234, completion_tokens=567),
        "2026-04-24T12:34:56Z",
    )

    assert renderable is not None
    console = Console(record=True, width=120)
    console.print(renderable)
    rendered = console.export_text()
    assert "done" in rendered
    assert "task-a" in rendered
    assert "1,234 prompt + 567 completion tokens" in rendered


def test_format_event_renders_integration_risk_evidence() -> None:
    renderable = _format_event(
        IntegrationRiskScored(
            task_slug="task-a",
            risk_level="high",
            claimed_files=("src/a.py",),
            changed_files=("src/a.py",),
            python_overlap_detected=True,
            overlap_evidence=(
                {
                    "_kind": "SymbolOverlap",
                    "symbol_name": "foo",
                    "symbol_type": "function",
                    "file_path": "src/a.py",
                    "task_line_range": None,
                    "target_line_range": None,
                },
            ),
        ),
        "2026-04-24T12:34:56Z",
    )

    assert renderable is not None
    console = Console(record=True, width=120)
    console.print(renderable)
    rendered = console.export_text()
    assert "risk=high" in rendered
    assert "claimed=1" in rendered
    assert "changed=1" in rendered
    assert "same-symbol edit: function foo in src/a.py" in rendered


def test_format_event_renders_semantic_gate_rejection_evidence() -> None:
    renderable = _format_event(
        SemanticGateRejected(
            task_slug="task-a",
            gate_name="same_symbol_edit",
            failure_class="same_symbol_edit",
            error_message="concurrent edit detected",
            evidence=(
                {
                    "_kind": "SymbolOverlap",
                    "symbol_name": "foo",
                    "symbol_type": "function",
                    "file_path": "src/a.py",
                    "task_line_range": None,
                    "target_line_range": None,
                },
            ),
        ),
        "2026-04-24T12:34:56Z",
    )

    assert renderable is not None
    console = Console(record=True, width=120)
    console.print(renderable)
    rendered = console.export_text()
    assert "gate err" in rendered
    assert "gate=same_symbol_edit" in rendered
    assert "class=same_symbol_edit" in rendered
    assert "concurrent edit detected" in rendered
    assert "same-symbol edit: function foo in src/a.py" in rendered


def test_infer_plan_name_ignores_stale_prior_run(tmp_path: Path) -> None:
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")
    emit_event(
        str(tmp_path),
        "dag_task_dispatched",
        "pane-a",
        plan_name="plan-a",
        task_slug="fix/a",
    )
    emit_event(str(tmp_path), "run_start", "run-plan-a", plan_name="plan-a")

    assert _infer_plan_name_from_active_tasks(str(tmp_path)) is None


# -- init-plan --


class TestInitPlan:
    """Tests for the dgov init-plan command."""

    def test_init_plan_creates_structure(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running init-plan myplan creates the expected structure."""
        monkeypatch.chdir(tmp_path)
        # Create the parent .dgov/plans directory
        plans_dir = tmp_path / ".dgov" / "plans"
        plans_dir.mkdir(parents=True)

        result = runner.invoke(cli, ["init-plan", "myplan"])
        assert result.exit_code == 0

        # Verify _root.toml exists
        root_toml = plans_dir / "myplan" / "_root.toml"
        assert root_toml.exists()

        # Verify tasks directory exists (default section)
        tasks_dir = plans_dir / "myplan" / "tasks"
        assert tasks_dir.exists()
        assert tasks_dir.is_dir()

        # Verify _root.toml content
        content = root_toml.read_text()
        assert 'name = "myplan"' in content
        assert 'sections = ["tasks"]' in content
        assert "copy or rename each _example.toml" in result.output

    def test_init_plan_custom_sections(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running init-plan with --sections creates custom sections."""
        monkeypatch.chdir(tmp_path)
        plans_dir = tmp_path / ".dgov" / "plans"
        plans_dir.mkdir(parents=True)

        result = runner.invoke(cli, ["init-plan", "myplan", "--sections", "core,extras"])
        assert result.exit_code == 0

        # Verify both section directories exist
        core_dir = plans_dir / "myplan" / "core"
        extras_dir = plans_dir / "myplan" / "extras"
        assert core_dir.exists()
        assert extras_dir.exists()

        # Verify _root.toml lists both sections
        root_toml = plans_dir / "myplan" / "_root.toml"
        content = root_toml.read_text()
        assert 'sections = ["core", "extras"]' in content

    def test_init_plan_already_exists(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running init-plan when directory exists fails with error."""
        monkeypatch.chdir(tmp_path)
        # Create the plan directory beforehand
        plan_dir = tmp_path / ".dgov" / "plans" / "myplan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "_root.toml").write_text("[plan]\n")

        result = runner.invoke(cli, ["init-plan", "myplan"])
        assert result.exit_code == 1
        assert "already exists" in result.output.lower()

    def test_init_plan_force_overwrites(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running init-plan with --force overwrites existing plan."""
        monkeypatch.chdir(tmp_path)
        # Create the plan directory with old content
        plan_dir = tmp_path / ".dgov" / "plans" / "myplan"
        plan_dir.mkdir(parents=True)
        old_content = '[plan]\nname = "oldname"\n'
        (plan_dir / "_root.toml").write_text(old_content)

        result = runner.invoke(cli, ["init-plan", "myplan", "--force"])
        assert result.exit_code == 0

        # Verify _root.toml was overwritten
        root_toml = plan_dir / "_root.toml"
        content = root_toml.read_text()
        assert 'name = "myplan"' in content

    def test_init_plan_from_inside_dgov_uses_repo_root(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        monkeypatch.chdir(dgov_dir)

        result = runner.invoke(cli, ["init-plan", "myplan"])
        assert result.exit_code == 0
        assert (tmp_path / ".dgov" / "plans" / "myplan" / "_root.toml").exists()
        assert not (tmp_path / ".dgov" / ".dgov" / "plans" / "myplan").exists()

    def test_init_plan_example_compiles_without_warnings(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        plans_dir = tmp_path / ".dgov" / "plans"
        plans_dir.mkdir(parents=True)

        result = runner.invoke(cli, ["init-plan", "myplan"])
        assert result.exit_code == 0

        example = plans_dir / "myplan" / "tasks" / "_example.toml"
        main = plans_dir / "myplan" / "tasks" / "main.toml"
        main.write_text(example.read_text())
        example.unlink()

        result = runner.invoke(cli, ["compile", str(plans_dir / "myplan"), "--dry-run"])
        assert result.exit_code == 0
        assert "WARNING" not in result.output


# -- run --


def test_format_event_settlement_retry() -> None:
    """Test that _format_event renders settlement_retry events correctly."""
    from rich.console import Console

    ev = SettlementRetry(task_slug="fix-lint", error="ruff check failed: E501 line too long")
    result = _format_event(ev, "2026-04-06T12:34:56Z", agents={})
    assert result is not None
    # Use a dummy console to capture output from the Table renderable
    console = Console(width=100)
    with console.capture() as capture:
        console.print(result)
    out = capture.get()
    assert "retry" in out
    assert "fix-lint" in out
    assert "ruff check failed" in out


def _run_only_plan_toml() -> str:
    """Return TOML for an a->b->c plan used in run --only tests."""
    return """
[tasks.a]
summary = "task a"
prompt = "do a"
commit_message = "a"
agent = "test-agent"
files = ["a.py"]

[tasks.b]
summary = "task b"
prompt = "do b"
commit_message = "b"
agent = "test-agent"
depends_on = ["a"]
files = ["b.py"]

[tasks.c]
summary = "task c"
prompt = "do c"
commit_message = "c"
agent = "test-agent"
depends_on = ["b"]
files = ["c.py"]
"""


class _RunOnlyFakeRunner:
    """Fake EventDagRunner for run --only tests."""

    def __init__(self, dag, **kwargs) -> None:
        self.dag = dag
        self._durations = {slug: 0.1 for slug in dag.tasks}

    @property
    def task_errors(self):
        return {}

    @property
    def task_durations(self):
        return self._durations

    async def run(self) -> dict[str, str]:
        return {slug: "merged" for slug in self.dag.tasks}


def _patch_run_only_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch dependencies for run --only tests (git, sentrux, runner, logging)."""
    monkeypatch.setattr("dgov.cli.run._ensure_git_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda *_: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda *_args, **_kwargs: {
            "degradation": False,
            "quality_before": 100,
            "quality_after": 100,
        },
    )
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _RunOnlyFakeRunner)
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)


def test_run_only_unknown_slug_exits(runner: CliRunner, tmp_path: Path) -> None:
    """Running with --only nonexistent exits with code 1 and error message."""
    with runner.isolated_filesystem(temp_dir=tmp_path):
        tasks_toml = """
[tasks.a]
summary = "do a"
prompt = "do a"
commit_message = "a"
agent = "test-agent"
files = ["a.py"]
"""
        compiled_path = compile_plan_tree(Path.cwd(), "unknown-slug-test", tasks_toml)
        result = runner.invoke(cli, ["run", str(compiled_path.parent), "--only", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "nonexistent" in result.output


def test_run_only_filters_plan(runner: CliRunner, tmp_path: Path) -> None:
    """Run with --only b on a->b->c plan: b is accepted, not 'not found'."""
    with runner.isolated_filesystem(temp_dir=tmp_path):
        tasks_toml = _run_only_plan_toml()
        compiled_path = compile_plan_tree(Path.cwd(), "filter-test", tasks_toml)
        plan_dir = compiled_path.parent

        monkeypatch = pytest.MonkeyPatch()
        _patch_run_only_deps(monkeypatch)

        try:
            # dgov run --only tasks/main.b should accept the slug
            target = "tasks/main.b"
            result = runner.invoke(cli, ["run", str(plan_dir), "--only", target])
            assert result.exit_code == 0
            assert "not found" not in result.output.lower()
            assert "status: complete" in result.output.lower()
        finally:
            monkeypatch.undo()


def test_run_rejects_uncompiled_plan(runner: CliRunner, tmp_path: Path) -> None:
    """Running with a single plan TOML file should fail with directory guidance."""
    with runner.isolated_filesystem(temp_dir=tmp_path):
        plan = Path("plan.toml")
        plan.write_text(
            '[plan]\nname = "test"\n\n'
            "[tasks.a]\n"
            'summary = "do a"\n'
            'prompt = "do a"\n'
            'commit_message = "a"\n'
        )
        result = runner.invoke(cli, ["run", str(plan)])

    assert result.exit_code == 1
    assert "requires a plan directory" in result.output.lower()
    assert "dgov run <plan-dir>" in result.output


# -- prune --


def _record_prune_scenario_tasks(project_root: str) -> None:
    """Record the four standard prune scenario tasks: abandoned, closed, pending, merged."""
    tasks = [
        WorkerTask(
            slug="abandoned-task",
            prompt="test",
            agent="test",
            project_root=project_root,
            worktree_path=project_root,
            branch_name="test",
            state=TaskState.ABANDONED,
        ),
        WorkerTask(
            slug="closed-task",
            prompt="test",
            agent="test",
            project_root=project_root,
            worktree_path=project_root,
            branch_name="test",
            state=TaskState.CLOSED,
        ),
        WorkerTask(
            slug="pending-task",
            prompt="test",
            agent="test",
            project_root=project_root,
            worktree_path=project_root,
            branch_name="test",
            state=TaskState.PENDING,
        ),
        WorkerTask(
            slug="merged-task",
            prompt="test",
            agent="test",
            project_root=project_root,
            worktree_path=project_root,
            branch_name="test",
            state=TaskState.MERGED,
        ),
    ]
    for task in tasks:
        record_runtime_artifact(project_root, task)


def _assert_remaining_slugs(project_root: str, expected_slugs: set[str]) -> None:
    """Assert that the remaining runtime artifacts match the expected slugs."""
    remaining = list_runtime_artifacts(project_root)
    remaining_slugs = {t["slug"] for t in remaining}
    assert remaining_slugs == expected_slugs


def test_prune_nothing_to_prune(runner: CliRunner, tmp_path: Path) -> None:
    """Prune on empty or non-historical tasks should report nothing to prune."""
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "Nothing to prune" in result.output


def test_prune_removes_historical_tasks(runner: CliRunner, tmp_path: Path) -> None:
    """Prune should remove abandoned and closed tasks, keeping pending/merged."""
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        _record_prune_scenario_tasks(td)

        result = runner.invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "Pruned 2 historical task(s)" in result.output

        _assert_remaining_slugs(td, {"pending-task", "merged-task"})


def test_prune_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    """Running prune twice should be idempotent — second run finds nothing."""
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        task = WorkerTask(
            slug="abandoned-task",
            prompt="test",
            agent="test",
            project_root=td,
            worktree_path=td,
            branch_name="test",
            state=TaskState.ABANDONED,
        )
        record_runtime_artifact(td, task)

        result1 = runner.invoke(cli, ["prune"])
        assert result1.exit_code == 0
        assert "Pruned 1 historical task(s)" in result1.output

        result2 = runner.invoke(cli, ["prune"])
        assert result2.exit_code == 0
        assert "Nothing to prune" in result2.output


# -----------------------------------------------------------------------------
# Integration risk telemetry rendering (dgov plan review)
# -----------------------------------------------------------------------------


class TestReviewIntegrationTelemetry:
    """Tests for rendering integration risk and candidate outcome in plan review."""

    def test_review_shows_integration_risk_when_present(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Human output should show risk level and overlap when present."""
        from dgov.plan_review import DiffStat, PlanReview, UnitReview

        unit = UnitReview(
            unit="tasks/main.a",
            summary="do a",
            status="deployed",
            commit_sha="abc1234",
            commit_message="feat: a",
            diff_stat=DiffStat(files_changed=1, insertions=2, deletions=0),
            landed_files=("src/a.py",),
            settlement="ok",
            integration_risk_level="high",
            integration_risk_detected=True,
            integration_candidate_passed=True,
            integration_claimed_files=("src/a.py",),
            integration_changed_files=("src/a.py", "tests/test_a.py"),
            integration_evidence=("same-symbol edit: function foo in src/a.py",),
        )
        review = PlanReview(
            plan_name="p",
            source_dir=None,
            last_run_ts=None,
            last_run_duration_s=None,
            units=[unit],
        )
        plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
        _patched_load_review(monkeypatch, review=review)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

        assert result.exit_code == 0, result.output
        assert "risk=high, overlap detected" in result.output
        assert "claimed" in result.output
        assert "src/a.py" in result.output
        assert "changed" in result.output
        assert "tests/test_a.py" in result.output
        assert "same-symbol edit: function foo in src/a.py" in result.output
        assert "candidate    passed" in result.output

    def test_review_shows_candidate_failure_when_present(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Human output should show candidate failure class when present."""
        from dgov.plan_review import PlanReview, UnitReview

        unit = UnitReview(
            unit="tasks/main.a",
            summary="do a",
            status="failed",
            reject_verdict="scope_violation",
            integration_risk_level="critical",
            integration_risk_detected=True,
            integration_candidate_passed=False,
            integration_failure_class="same_symbol_edit",
            integration_gate_name="same_symbol_edit",
            integration_error="concurrent edit detected",
            integration_evidence=("same-symbol edit: function foo in src/a.py",),
        )
        review = PlanReview(
            plan_name="p",
            source_dir=None,
            last_run_ts=None,
            last_run_duration_s=None,
            units=[unit],
        )
        plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
        _patched_load_review(monkeypatch, review=review)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

        assert result.exit_code == 1  # Failed units exit non-zero
        assert "risk=critical, overlap detected" in result.output
        assert "candidate    same_symbol_edit" in result.output
        assert "gate         same_symbol_edit" in result.output
        assert "concurrent edit detected" in result.output
        assert "same-symbol edit: function foo in src/a.py" in result.output

    def test_review_omits_integration_when_not_present(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Human output should not show integration fields when not present."""
        from dgov.plan_review import DiffStat, PlanReview, UnitReview

        unit = UnitReview(
            unit="tasks/main.a",
            summary="do a",
            status="deployed",
            commit_sha="abc1234",
            commit_message="feat: a",
            diff_stat=DiffStat(files_changed=1, insertions=2, deletions=0),
            landed_files=("src/a.py",),
            settlement="ok",
            # No integration fields set (defaults)
        )
        review = PlanReview(
            plan_name="p",
            source_dir=None,
            last_run_ts=None,
            last_run_duration_s=None,
            units=[unit],
        )
        plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
        _patched_load_review(monkeypatch, review=review)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

        assert result.exit_code == 0, result.output
        # Should not have integration lines when not present
        assert "risk=" not in result.output
        assert "overlap detected" not in result.output
        assert "candidate    passed" not in result.output

    def test_review_json_includes_integration_fields(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON output should include integration telemetry fields."""
        review = _json_integration_review()
        plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
        _patched_load_review(monkeypatch, review=review)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["--json", "plan", "review", str(plan_dir)])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        unit_data = data["units"][0]
        assert unit_data["integration_risk_level"] == "medium"
        assert unit_data["integration_risk_detected"] is True
        assert unit_data["integration_candidate_passed"] is True
        assert unit_data["integration_failure_class"] is None
        assert unit_data["integration_claimed_files"] == ["src/a.py"]
        assert unit_data["integration_changed_files"] == ["src/a.py"]
        assert unit_data["integration_gate_name"] is None
        assert unit_data["integration_error"] is None
        assert unit_data["integration_evidence"] == ["same-symbol edit: function foo in src/a.py"]
