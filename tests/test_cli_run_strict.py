"""Tests for strict dgov run requirements (plan directory enforcement)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import click
import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.event_types import RunCompleted

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


def _write_plan_tree(tmp_path: Path, name: str = "compiled") -> Path:
    plan_dir = tmp_path / ".dgov" / "plans" / name
    task_dir = plan_dir / "tasks"
    task_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        f'[plan]\nname = "{name}"\nsummary = "test"\nsections = ["tasks"]\n'
    )
    (task_dir / "main.toml").write_text(
        """
[tasks.a]
summary = "do a"
prompt = "do a"
commit_message = "a"
files.edit = ["src/a.py"]
"""
    )
    return plan_dir


def _write_compiled(plan_dir: Path, name: str = "compiled") -> Path:
    compiled = plan_dir / "_compiled.toml"
    compiled.write_text(
        f'[plan]\nname = "{name}"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )
    return compiled


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_committed_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _git_head(repo)


def _fake_event_runner(
    results: dict[str, str],
    *,
    task_errors: dict[str, str] | None = None,
    task_durations: dict[str, float] | None = None,
) -> type[object]:
    run_results = dict(results)
    run_errors = dict(task_errors or {})
    run_durations = dict(task_durations or {})

    class _Runner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        @property
        def task_errors(self) -> dict[str, str]:
            return run_errors

        @property
        def task_durations(self) -> dict[str, float]:
            return run_durations

        async def run(self) -> dict[str, str]:
            return run_results

    return _Runner


def _degraded_sentrux_result(baseline_quality: int | None) -> dict[str, object]:
    return {
        "degradation": True,
        "quality_before": baseline_quality,
        "quality_after": 90,
    }


def _structural_offender_degraded_sentrux_result(
    baseline_quality: int | None,
) -> dict[str, object]:
    """Return a degraded sentrux result payload with structural offenders."""
    result = _degraded_sentrux_result(baseline_quality)
    result["structural_offenders"] = {
        "commit_sha": "abc123",
        "complex_functions": [
            {
                "path": "src/dgov/runner.py",
                "qualname": "_merge",
                "lineno": 100,
                "cyclomatic": 12,
            }
        ],
        "cog_complex_functions": [],
        "long_functions": [],
    }
    return result


def _mock_sentrux_baseline_writer(
    project_root: Path,
    *,
    quality_signal: float | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a mock run_sentrux that writes a baseline file with quality 100 or quality_signal."""

    def _mock_run_sentrux(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["gate", "--save", str(project_root)]
        sentrux_dir = project_root / ".sentrux"
        sentrux_dir.mkdir(parents=True, exist_ok=True)
        if quality_signal is not None:
            (sentrux_dir / "baseline.json").write_text(f'{{"quality_signal": {quality_signal}}}\n')
            stdout = f"Quality: {quality_signal}\n"
        else:
            (sentrux_dir / "baseline.json").write_text('{"quality": 100}\n')
            stdout = "Quality: 100\n"
        return subprocess.CompletedProcess(
            ["sentrux", *args],
            0,
            stdout=stdout,
            stderr="",
        )

    return _mock_run_sentrux


def _install_degraded_successful_run_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_events: list[object],
    baseline_quality: int = 100,
) -> None:
    """Install monkeypatches for a successful but degraded dgov run."""
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner({"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr(
        "dgov.cli.run._require_sentrux_baseline",
        lambda project_root: baseline_quality,
    )
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: _degraded_sentrux_result(
            baseline_quality
        ),
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)

    def _capture_fn(session_root: str, event: object, pane: str = "", **kwargs: object) -> None:
        captured_events.append(event)

    monkeypatch.setattr("dgov.cli.run.emit_event", _capture_fn)
    monkeypatch.chdir(tmp_path)


def _assert_degraded_run_completed_event(
    captured_events: list[object],
    expected_plan_name: str,
    expected_branch: str,
    expected_baseline_quality: int = 100,
    expected_run_source: str = "manual",
) -> None:
    """Find and assert the run_completed event payload for a degraded run."""
    run_completed_events = [
        e for e in captured_events if getattr(e, "event_type", None) == "run_completed"
    ]
    assert len(run_completed_events) == 1

    run_completed = cast(RunCompleted, run_completed_events[0])
    assert run_completed.pane == expected_plan_name
    assert run_completed.plan_name == expected_plan_name
    assert run_completed.run_status == "degraded"
    assert run_completed.run_source == expected_run_source
    assert isinstance(run_completed.duration_s, float)

    sentrux = json.loads(run_completed.sentrux)
    assert sentrux == {
        "degradation": True,
        "quality_before": expected_baseline_quality,
        "quality_after": 90,
        "branch_verification": {
            "status": "clean",
            "base": expected_branch,
            "head": expected_branch,
            "changed_files": 0,
        },
    }


def test_run_rejects_compiled_file_input(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    compiled = _write_compiled(plan_dir, "compiled")

    result = runner.invoke(cli, ["run", str(compiled)])

    assert result.exit_code != 0
    assert "requires a plan directory" in result.output.lower()
    assert "dgov run <plan-dir>" in result.output


def test_run_plan_dir_compiles_before_execution(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    captured: dict[str, object] = {}

    def _capture_compile(path: Path) -> None:
        captured["compiled"] = path
        _write_compiled(path, "compiled")

    def _capture_run(plan_file: str, project_root: str, **kwargs: object) -> None:
        captured["plan_file"] = plan_file
        captured["project_root"] = project_root
        captured["kwargs"] = kwargs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _capture_compile)
    monkeypatch.setattr("dgov.cli.run.run_compiled_plan", _capture_run)

    result = runner.invoke(cli, ["run", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert captured["compiled"] == plan_dir
    assert captured["plan_file"] == str(plan_dir / "_compiled.toml")
    assert captured["project_root"] == str(tmp_path)
    assert captured["kwargs"] == {
        "restart": False,
        "continue_failed": False,
        "only": None,
        "plan_dir": plan_dir,
        "yes": False,
        "stream": False,
        "verbose": False,
    }


def test_run_compiled_plan_rejects_department_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import run_compiled_plan

    dgov_dir = tmp_path / ".dgov"
    plan_dir = dgov_dir / "plans" / "constitution"
    plan_dir.mkdir(parents=True)
    (dgov_dir / "project.toml").write_text(
        '[departments]\nCore = ["src/dgov/kernel.py"]\n',
        encoding="utf-8",
    )
    compiled = plan_dir / "_compiled.toml"
    compiled.write_text(
        '[plan]\nname = "constitution"\nsource_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "Do a"\n'
        'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
        'commit_message = "a"\n'
        'files = ["src/dgov/kernel.py"]\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    with pytest.raises(click.ClickException, match="Constitutional violation"):
        run_compiled_plan(
            str(compiled),
            str(tmp_path),
            plan_dir=plan_dir,
        )


def _install_successful_run_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Install monkeypatches for a successful dgov run with no degradation."""
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner({"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)


def test_run_auto_bootstraps_dgov_only_repo(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-bootstrap creates a commit when repo has no HEAD (dgov-only repo)."""
    plan_dir = _write_plan_tree(tmp_path, "bootstrap")
    _write_compiled(plan_dir, "compiled")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    _install_successful_run_patches(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.exit_code == 0
    assert "created bootstrap commit" in result.output.lower()
    assert head.returncode == 0


def _fake_failed_event_runner() -> type[object]:
    """Return a runner class that simulates task failure with errors."""
    return _fake_event_runner(
        {"a": "failed"},
        task_errors={"a": "boom"},
        task_durations={"a": 0.1},
    )


def test_run_returns_nonzero_on_failed_plan(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run exits with code 1 and reports task errors when a plan task fails."""
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    _init_committed_repo(tmp_path)

    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _fake_failed_event_runner())
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "status: failed" in result.output
    assert "boom" in result.output


def test_run_auto_creates_bootstrap_commit_in_headless(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.sentrux_available", lambda: True)
    monkeypatch.setattr("dgov.cli.run.run_sentrux", _mock_sentrux_baseline_writer(tmp_path))
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner(results={"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "created bootstrap commit from current working tree" in result.output.lower()
    assert "bootstrapping baseline" in result.output.lower()
    assert "baseline saved" in result.output.lower()

    git_log = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "chore: bootstrap repo for dgov" in git_log
    assert (tmp_path / ".sentrux" / "baseline.json").exists()


def test_run_bootstraps_missing_sentrux_baseline(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    _init_committed_repo(tmp_path)

    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.sentrux_available", lambda: True)
    monkeypatch.setattr(
        "dgov.cli.run.run_sentrux",
        _mock_sentrux_baseline_writer(tmp_path, quality_signal=0.91),
    )
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner({"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "bootstrapping baseline" in result.output.lower()
    assert "baseline saved" in result.output.lower()
    assert "[sentrux] baseline quality: 9100" in result.output.lower()
    assert (tmp_path / ".sentrux" / "baseline.json").exists()


def _commit_sentrux_baseline(tmp_path: Path) -> tuple[Path, str]:
    _init_committed_repo(tmp_path)
    sentrux_dir = tmp_path / ".sentrux"
    sentrux_dir.mkdir()
    (sentrux_dir / "baseline.json").write_text('{"quality": 100}\n')
    subprocess.run(["git", "add", ".sentrux/baseline.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "save baseline"], cwd=tmp_path, check=True)
    return sentrux_dir, _git_head(tmp_path)


def _mock_clean_sentrux_save(
    sentrux_dir: Path,
    captured: dict[str, object],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _mock_run_sentrux(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        (sentrux_dir / "baseline.json").write_text('{"quality": 100}\n')
        return subprocess.CompletedProcess(
            ["sentrux", *args],
            0,
            stdout="Quality: 100\n",
            stderr="",
        )

    return _mock_run_sentrux


def _latest_commit_subject(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_refresh_sentrux_baseline_after_clean_run_commits_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import _refresh_sentrux_baseline_after_clean_run

    sentrux_dir, accepted_head = _commit_sentrux_baseline(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "dgov.cli.run.run_sentrux", _mock_clean_sentrux_save(sentrux_dir, captured)
    )
    monkeypatch.chdir(tmp_path)

    _refresh_sentrux_baseline_after_clean_run(".")

    assert captured["args"] == ["gate", "--save", str(tmp_path.resolve())]
    assert captured["timeout"] == 30.0

    metadata = json.loads((sentrux_dir / "dgov-baseline.json").read_text())
    assert metadata["accepted_head"] == accepted_head
    assert metadata["quality"] == 100
    assert _latest_commit_subject(tmp_path) == "Refresh sentrux baseline"


def test_refresh_sentrux_baseline_after_clean_run_rejects_dirty_source_tree(
    tmp_path: Path,
) -> None:
    from dgov.sentrux_baseline import (
        SentruxBaselineRefreshError,
        refresh_sentrux_baseline_after_clean_run,
    )

    sentrux_dir, _accepted_head = _commit_sentrux_baseline(tmp_path)
    (tmp_path / "README.md").write_text("dirty\n")
    calls = 0

    def _unexpected_sentrux(
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(["sentrux", *args], 0, stdout="", stderr="")

    with pytest.raises(SentruxBaselineRefreshError, match="non-baseline"):
        refresh_sentrux_baseline_after_clean_run(
            str(tmp_path),
            run_sentrux=_unexpected_sentrux,
        )

    assert calls == 0
    assert not (sentrux_dir / "dgov-baseline.json").exists()


def _clean_complete_artifacts() -> object:
    from dgov.cli.run import PlanRunArtifacts

    class _Runner:
        def __init__(self) -> None:
            self.task_errors: dict[str, str] = {}
            self.task_durations: dict[str, float] = {}

    return PlanRunArtifacts(
        runner=cast(Any, _Runner()),
        results={"a": "merged"},
        duration=timedelta(seconds=1),
        gate_result={"degradation": False},
        branch_result={"status": "clean"},
        completed_gate_result={
            "degradation": False,
            "branch_verification": {"status": "clean"},
        },
        token_usage={},
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )


def _compiled_test_dag(plan_dir: Path) -> object:
    from dgov.dag_parser import DagDefinition, DagTaskSpec

    return DagDefinition(
        name="compiled",
        dag_file=str(plan_dir / "_compiled.toml"),
        tasks={"a": DagTaskSpec(slug="a", summary="do a")},
    )


def _install_finalize_refresh_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    calls: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(
        "dgov.cli.run.is_plan_complete",
        lambda project_root, plan_name, tasks: (
            project_root == str(tmp_path.resolve()) and plan_name == "compiled" and tasks == {"a"}
        ),
    )
    monkeypatch.setattr(
        "dgov.cli.run._refresh_sentrux_baseline_after_clean_run",
        lambda project_root: calls.append(("refresh", project_root)),
    )
    monkeypatch.setattr(
        "dgov.cli.run.archive_plan",
        lambda archive_target: (
            calls.append(("archive", str(archive_target))) or archive_target / "archive"
        ),
    )


def test_finalize_refreshes_sentrux_baseline_before_archiving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import _finalize_plan_run

    plan_dir = _write_plan_tree(tmp_path, "compiled")
    calls: list[tuple[str, str]] = []

    monkeypatch.chdir(tmp_path)
    _install_finalize_refresh_patches(monkeypatch, tmp_path, calls)
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("dgov.cli.run.emit_event", lambda *args, **kwargs: None)

    status = _finalize_plan_run(
        cast(Any, _clean_complete_artifacts()),
        dag=cast(Any, _compiled_test_dag(plan_dir)),
        project_root=".",
        plan_file=str(plan_dir / "_compiled.toml"),
        only=None,
        plan_dir=plan_dir,
        verbose=False,
        stream=False,
    )

    assert status == "complete"
    assert calls[:2] == [
        ("refresh", str(tmp_path.resolve())),
        ("archive", str(plan_dir)),
    ]


def test_record_run_completion_uses_runner_run_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import PlanRunSummary, _record_run_completion

    artifacts = cast(Any, _clean_complete_artifacts())
    artifacts.runner.run_source = "workshop"
    captured_events: list[RunCompleted] = []

    monkeypatch.setenv("DGOV_RUN_SOURCE", "invalid source")
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("dgov.cli.run._maybe_archive_completed_plan", lambda **kwargs: None)
    monkeypatch.setattr(
        "dgov.cli.run.emit_event",
        lambda _project_root, event: captured_events.append(event),
    )

    _record_run_completion(
        project_root=str(tmp_path),
        dag=cast(Any, _compiled_test_dag(tmp_path)),
        plan_file=str(tmp_path / "_compiled.toml"),
        artifacts=artifacts,
        summary=PlanRunSummary(
            run_status="complete",
            failed=[],
            abandoned=[],
            skipped=[],
            succeeded=["a"],
            task_errors={},
        ),
        only=None,
        plan_dir=None,
    )

    assert captured_events[0].run_source == "workshop"


def test_run_reports_degraded_when_final_sentrux_compare_degrades(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")

    _init_committed_repo(tmp_path)

    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner({"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: _degraded_sentrux_result(
            baseline_quality
        ),
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "status: degraded" in result.output
    assert "sentrux: architectural degradation detected." in result.output.lower()


def test_run_emits_run_completed_event_with_degraded_status(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that dgov run emits run_completed event with final status and sentrux payload."""
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    head = _init_committed_repo(tmp_path)
    captured_events: list[object] = []

    _install_degraded_successful_run_patches(monkeypatch, tmp_path, captured_events)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    _assert_degraded_run_completed_event(captured_events, "compiled", head)


def test_run_completed_event_records_env_run_source(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    head = _init_committed_repo(tmp_path)
    captured_events: list[object] = []

    monkeypatch.setenv("DGOV_RUN_SOURCE", "workshop")
    _install_degraded_successful_run_patches(monkeypatch, tmp_path, captured_events)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    _assert_degraded_run_completed_event(
        captured_events,
        "compiled",
        head,
        expected_run_source="workshop",
    )


def _mock_branch_verification_failure(project_root: str, config: object) -> dict[str, object]:
    """Return a failed branch verification result for monkeypatching."""
    return {
        "status": "failed",
        "changed_files": 2,
        "error": "Type check failure",
    }


def test_run_returns_nonzero_on_branch_verification_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    _init_committed_repo(tmp_path)

    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner(results={"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr(
        "dgov.cli.run._branch_verification_gate",
        _mock_branch_verification_failure,
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "status: degraded" in result.output
    assert "branch verification: Type check failure" in result.output


def test_run_reports_structural_offenders_when_sentrux_degrades(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")
    _init_committed_repo(tmp_path)

    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", lambda path: None)
    monkeypatch.setattr(
        "dgov.cli.run.EventDagRunner",
        _fake_event_runner({"a": "merged"}, task_durations={"a": 0.1}),
    )
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality, *_, **__: (
            _structural_offender_degraded_sentrux_result(baseline_quality)
        ),
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Likely structural offenders at abc123" in result.output


def test_normalize_sentrux_assessment() -> None:
    """_normalize_sentrux_assessment should map assessment fields correctly."""
    from dgov.cli.run import _normalize_sentrux_assessment

    class FakeAssessment:
        def __init__(self) -> None:
            self.should_fail = True
            self.warning = "warn"
            self.error = "err"
            self.current_report = {"b": 2}

    degradation, offenders, error, warning = _normalize_sentrux_assessment(
        FakeAssessment(),  # type: ignore
        {"a": 1},
        False,
    )
    assert degradation is True
    assert offenders == {"b": 2}
    assert error == "err"
    assert warning == "warn"

    degradation2, offenders2, error2, warning2 = _normalize_sentrux_assessment(
        None, {"a": 1}, True
    )
    assert degradation2 is True
    assert offenders2 == {"a": 1}
    assert error2 is None
    assert warning2 is None


def test_clean_head_worktree_isolates_from_dirty_state(tmp_path: Path) -> None:
    """_clean_head_worktree yields a checkout at HEAD, ignoring dirty working-tree changes."""
    from dgov.cli.run import _clean_head_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    tracked = repo / "tracked.py"
    tracked.write_text("x = 1\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo,
        check=True,
        env=env,
    )

    tracked.write_text("x = 2  # uncommitted\n")
    (repo / "untracked.py").write_text("y = 999\n")

    with _clean_head_worktree(str(repo)) as scan_dir:
        assert (scan_dir / "tracked.py").read_text() == "x = 1\n"
        assert not (scan_dir / "untracked.py").exists()
        snapshot = scan_dir

    assert not snapshot.exists()
    assert tracked.read_text() == "x = 2  # uncommitted\n"
    assert (repo / "untracked.py").exists()
