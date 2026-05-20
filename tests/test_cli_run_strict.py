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

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


def _write_plan_tree(tmp_path: Path, name: str = "compiled") -> Path:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir(parents=True, exist_ok=True)
    project_toml = dgov_dir / "project.toml"
    if not project_toml.exists():
        project_toml.write_text(_provider_project_toml(), encoding="utf-8")
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
        'agent = "test-agent"\n'
    )
    return compiled


def _provider_project_toml(extra: str = "") -> str:
    return f"""
[project]
provider = "test-provider"
{extra}

[providers.test-provider]
default_agent = "provider/model-name"
base_url = "https://provider.example.com/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
"""


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


def _commit_all(repo: Path, message: str = "add files") -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_dirty_worker_files_counts_rename_source_into_dgov(tmp_path: Path) -> None:
    from dgov.cli.run import _dirty_worker_files

    _init_committed_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / ".dgov").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n")
    (tmp_path / ".dgov" / "keep").write_text("state\n")
    subprocess.run(["git", "add", "src/foo.py", ".dgov/keep"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add files"], cwd=tmp_path, check=True)
    subprocess.run(["git", "mv", "src/foo.py", ".dgov/foo.py"], cwd=tmp_path, check=True)

    assert _dirty_worker_files(str(tmp_path)) == ["src/foo.py", ".dgov/foo.py"]


def test_run_blocks_dirty_worktree_with_shared_status(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_committed_repo(tmp_path)
    plan_dir = _write_plan_tree(tmp_path, "dirty-block")
    _write_compiled(plan_dir, "dirty-block")
    _commit_all(tmp_path, "add plan")
    (tmp_path / "README.md").write_text("dirty\n")

    def _unexpected_baseline(_project_root: str) -> int:
        pytest.fail("dirty worktree block should happen before sentrux baseline checks")

    def _unexpected_compile(_path: Path) -> None:
        pytest.fail("dirty worktree block should happen before plan compilation")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _unexpected_compile)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", _unexpected_baseline)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "dispatch_status: blocked_by_dirty_worktree" in result.output
    assert "dirty_count: 1" in result.output
    assert "README.md" in result.output
    assert "dirty_omitted: 0" in result.output


def test_run_json_bounds_dirty_worktree_paths(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_committed_repo(tmp_path)
    plan_dir = _write_plan_tree(tmp_path, "dirty-json")
    _write_compiled(plan_dir, "dirty-json")
    _commit_all(tmp_path, "add plan")
    for index in range(12):
        (tmp_path / f"dirty-{index:02d}.txt").write_text("dirty\n")

    def _unexpected_baseline(_project_root: str) -> int:
        pytest.fail("dirty worktree block should happen before sentrux baseline checks")

    def _unexpected_compile(_path: Path) -> None:
        pytest.fail("dirty worktree block should happen before plan compilation")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _unexpected_compile)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", _unexpected_baseline)

    result = runner.invoke(
        cli,
        ["run", str(plan_dir)],
        env={"DGOV_JSON": "1"},
        catch_exceptions=False,
    )

    data = json.loads(result.output)
    assert result.exit_code == 1
    assert data["status"] == "blocked_by_dirty_worktree"
    assert data["dispatch_status"] == "blocked_by_dirty_worktree"
    assert data["dirty_count"] == 12
    assert len(data["dirty_paths"]) == 10
    assert data["dirty_omitted"] == 2


def test_run_allows_dirty_dgov_generated_metadata_before_compile(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_committed_repo(tmp_path)
    plan_dir = _write_plan_tree(tmp_path, "dirty-dgov")
    _write_compiled(plan_dir, "dirty-dgov")
    _commit_all(tmp_path, "add plan")
    (tmp_path / ".dgov" / "runtime").mkdir()
    (tmp_path / ".dgov" / "runtime" / "run.jsonl").write_text('{"status": "dirty"}\n')
    captured: dict[str, Path] = {}

    def _reached_compile(path: Path) -> None:
        captured["path"] = path
        raise click.exceptions.Exit(code=7)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _reached_compile)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 7
    assert captured["path"] == plan_dir
    assert "blocked_by_dirty_worktree" not in result.output


def test_run_blocks_dirty_dgov_plan_source_before_compile(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_committed_repo(tmp_path)
    plan_dir = _write_plan_tree(tmp_path, "dirty-source")
    _write_compiled(plan_dir, "dirty-source")
    _commit_all(tmp_path, "add plan")
    (plan_dir / "_root.toml").write_text('[plan]\nname = "dirty-source"\nmodified = true\n')

    def _unexpected_baseline(_project_root: str) -> int:
        pytest.fail("dirty worktree block should happen before sentrux baseline checks")

    def _unexpected_compile(_path: Path) -> None:
        pytest.fail("dirty worktree block should happen before plan compilation")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _unexpected_compile)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", _unexpected_baseline)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "dispatch_status: blocked_by_dirty_worktree" in result.output
    assert ".dgov/plans/dirty-source/_root.toml" in result.output


def test_branch_changed_source_files_decodes_unicode_path(tmp_path: Path) -> None:
    from dgov.cli.run import _git_stdout
    from dgov.cli.run_checks import _branch_changed_source_files

    _init_committed_repo(tmp_path)
    name = "caf\u00e9.py"
    (tmp_path / name).write_text("x = 1\n")
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add unicode"], cwd=tmp_path, check=True)
    base = _git_head(tmp_path)
    (tmp_path / name).write_text("x = 2\n")
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change unicode"], cwd=tmp_path, check=True)

    assert _branch_changed_source_files(
        str(tmp_path),
        base,
        (".py",),
        git_stdout=_git_stdout,
    ) == [name]


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

    run_completed = cast(Any, run_completed_events[0])
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


def test_run_rejects_compiled_file_input(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    compiled = _write_compiled(plan_dir, "compiled")
    monkeypatch.chdir(tmp_path)

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


def test_run_resolves_relative_plan_path_from_current_directory(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    captured: dict[str, object] = {}

    def _capture_compile(path: Path) -> None:
        captured["compiled"] = path
        _write_compiled(path, "compiled")

    def _capture_run(plan_file: str, project_root: str, **kwargs: object) -> None:
        captured["plan_file"] = plan_file
        captured["project_root"] = project_root
        captured["kwargs"] = kwargs

    monkeypatch.chdir(subdir)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _capture_compile)
    monkeypatch.setattr("dgov.cli.run.run_compiled_plan", _capture_run)

    result = runner.invoke(cli, ["run", "../.dgov/plans/compiled"])

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


def test_run_rejects_plan_path_outside_project_root_before_compile(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside_dir = tmp_path.parent
    calls: list[Path] = []

    def _capture_compile(path: Path) -> None:
        calls.append(path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _capture_compile)

    for bad_path in ("..", str(outside_dir)):
        result = runner.invoke(cli, ["run", bad_path])

        assert result.exit_code == 1
        assert "plan path must stay under project root" in result.output

    assert calls == []


def test_run_reports_missing_plan_path_after_containment_check(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def _capture_compile(path: Path) -> None:
        calls.append(path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run.compile_plan_for_run", _capture_compile)

    result = runner.invoke(cli, ["run", ".dgov/plans/missing"])

    assert result.exit_code == 1
    assert "plan path not found" in result.output
    assert "pass a plan directory under this project root" in result.output
    assert calls == []


def test_run_compiled_plan_rejects_plan_file_outside_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import run_compiled_plan

    project_root = tmp_path / "project"
    outside_plan_dir = tmp_path / "outside"
    project_root.mkdir()
    outside_plan_dir.mkdir()
    compiled_path = outside_plan_dir / "_compiled.toml"

    def _fail_compile(*args: object, **kwargs: object) -> None:
        pytest.fail("outside compiled plan reached DAG compilation")

    monkeypatch.setattr("dgov.cli.run._compile_dag_for_run", _fail_compile)

    with pytest.raises(click.ClickException, match="plan file must stay under project root"):
        run_compiled_plan(
            str(compiled_path),
            str(project_root),
            plan_dir=outside_plan_dir,
        )


def test_run_compiled_plan_rejects_department_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import run_compiled_plan

    dgov_dir = tmp_path / ".dgov"
    plan_dir = dgov_dir / "plans" / "constitution"
    plan_dir.mkdir(parents=True)
    (dgov_dir / "project.toml").write_text(
        _provider_project_toml() + '\n[departments]\nCore = ["src/dgov/kernel.py"]\n',
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


def test_run_compiled_plan_rejects_missing_provider_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dgov.cli.run import run_compiled_plan

    dgov_dir = tmp_path / ".dgov"
    plan_dir = dgov_dir / "plans" / "missing-provider"
    plan_dir.mkdir(parents=True)
    (dgov_dir / "project.toml").write_text("[project]\n", encoding="utf-8")
    compiled = plan_dir / "_compiled.toml"
    compiled.write_text(
        '[plan]\nname = "missing-provider"\nsource_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "Do a"\n'
        'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
        'commit_message = "a"\n'
        'agent = "some/model"\n'
        'files = ["README.md"]\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    with pytest.raises(click.ClickException, match="No provider for task"):
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
    _sentrux_dir: Path,
    captured: dict[str, object],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _mock_run_sentrux(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        scan_root = Path(args[-1])
        captured["scan_root"] = str(scan_root)
        captured["scan_head"] = _git_head(scan_root)
        scan_sentrux_dir = scan_root / ".sentrux"
        scan_sentrux_dir.mkdir(parents=True, exist_ok=True)
        (scan_sentrux_dir / "baseline.json").write_text('{"quality": 100}\n')
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

    assert cast(list[str], captured["args"])[:2] == ["gate", "--save"]
    assert captured["timeout"] == 30.0
    assert captured["scan_root"] != str(tmp_path.resolve())
    assert captured["scan_head"] == accepted_head

    metadata = json.loads((sentrux_dir / "dgov-baseline.json").read_text())
    assert metadata["accepted_head"] == accepted_head
    assert metadata["quality"] == 100
    assert _latest_commit_subject(tmp_path) == "Refresh sentrux baseline"


def test_refresh_sentrux_baseline_after_clean_run_allows_dgov_run_metadata(
    tmp_path: Path,
) -> None:
    from dgov.sentrux_baseline import refresh_sentrux_baseline_after_clean_run

    _init_committed_repo(tmp_path)
    sentrux_dir = tmp_path / ".sentrux"
    plan_dir = tmp_path / ".dgov" / "sops" / "plan_hello"
    deployed_log = tmp_path / ".dgov" / "plans" / "deployed.jsonl"
    compiled_plan = plan_dir / "_compiled.toml"
    deployed_log.parent.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    sentrux_dir.mkdir()
    (sentrux_dir / "baseline.json").write_text('{"quality": 90}\n')
    deployed_log.write_text('{"plan":"hello","unit":"old","sha":"abc","ts":"old"}\n')
    compiled_plan.write_text('[plan]\nname = "hello"\n')
    subprocess.run(
        [
            "git",
            "add",
            ".sentrux/baseline.json",
            ".dgov/plans/deployed.jsonl",
            ".dgov/sops/plan_hello/_compiled.toml",
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "save dgov state"], cwd=tmp_path, check=True)
    accepted_head = _git_head(tmp_path)
    deployed_log.write_text(deployed_log.read_text() + '{"plan":"hello","unit":"new"}\n')
    compiled_plan.write_text('[plan]\nname = "hello"\nsource_mtime_max = "now"\n')
    captured: dict[str, object] = {}

    committed = refresh_sentrux_baseline_after_clean_run(
        str(tmp_path),
        run_sentrux=_mock_clean_sentrux_save(sentrux_dir, captured),
    )

    assert committed is True
    assert captured["scan_head"] == accepted_head
    metadata = json.loads((sentrux_dir / "dgov-baseline.json").read_text())
    assert metadata["accepted_head"] == accepted_head
    assert _latest_commit_subject(tmp_path) == "Refresh sentrux baseline"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "M .dgov/plans/deployed.jsonl" in status
    assert "M .dgov/sops/plan_hello/_compiled.toml" in status


def test_refresh_sentrux_baseline_after_clean_run_allows_scope_ignored_uv_lock(
    tmp_path: Path,
) -> None:
    from dgov.sentrux_baseline import refresh_sentrux_baseline_after_clean_run

    sentrux_dir, accepted_head = _commit_sentrux_baseline(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    captured: dict[str, object] = {}

    committed = refresh_sentrux_baseline_after_clean_run(
        str(tmp_path),
        run_sentrux=_mock_clean_sentrux_save(sentrux_dir, captured),
    )

    assert committed is True
    assert captured["scan_head"] == accepted_head
    metadata = json.loads((sentrux_dir / "dgov-baseline.json").read_text())
    assert metadata["accepted_head"] == accepted_head
    assert _latest_commit_subject(tmp_path) == "Refresh sentrux baseline"
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "?? uv.lock" in status


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


def test_refresh_sentrux_baseline_rejects_renamed_source_path(
    tmp_path: Path,
) -> None:
    from dgov.sentrux_baseline import (
        SentruxBaselineRefreshError,
        refresh_sentrux_baseline_after_clean_run,
    )

    sentrux_dir, _accepted_head = _commit_sentrux_baseline(tmp_path)
    source = tmp_path / "src.py"
    source.write_text("x = 1\n")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add source"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "mv", "src.py", ".sentrux/dgov-baseline.json"],
        cwd=tmp_path,
        check=True,
    )
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

    with pytest.raises(SentruxBaselineRefreshError, match=r"src\.py"):
        refresh_sentrux_baseline_after_clean_run(
            str(tmp_path),
            run_sentrux=_unexpected_sentrux,
        )

    assert calls == 0
    assert (sentrux_dir / "dgov-baseline.json").read_text() == "x = 1\n"


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
    captured_events: list[object] = []

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

    assert cast(Any, captured_events[0]).run_source == "workshop"


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
