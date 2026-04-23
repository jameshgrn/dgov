"""Tests for strict dgov run requirements (plan directory enforcement)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

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
    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", _capture_compile)
    monkeypatch.setattr("dgov.cli.run._cmd_run_plan", _capture_run)

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


def test_run_auto_bootstraps_dgov_only_repo(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "bootstrap")
    _write_compiled(plan_dir, "compiled")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def task_errors(self):
            return {}

        @property
        def task_durations(self):
            return {}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _Runner)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality: {
            "degradation": False,
            "quality_before": baseline_quality,
            "quality_after": baseline_quality,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

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


def test_run_returns_nonzero_on_failed_plan(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def task_errors(self):
            return {"a": "boom"}

        @property
        def task_durations(self):
            return {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "failed"}

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _Runner)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality: {
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
    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run._sentrux_available", lambda: True)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "created bootstrap commit from current working tree" in result.output.lower()
    assert "no sentrux baseline found" in result.output.lower()

    git_log = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "chore: bootstrap repo for dgov" in git_log


def test_run_requires_sentrux_baseline(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run._sentrux_available", lambda: True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "no sentrux baseline found" in result.output.lower()
    assert "dgov sentrux gate-save" in result.output


def test_run_reports_degraded_when_final_sentrux_compare_degrades(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def task_errors(self):
            return {}

        @property
        def task_durations(self):
            return {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _Runner)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality: {
            "degradation": True,
            "quality_before": baseline_quality,
            "quality_after": 90,
        },
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

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def task_errors(self):
            return {}

        @property
        def task_durations(self):
            return {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

    captured_events: list[dict] = []

    def _capture_emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
        captured_events.append({"event": event, "pane": pane, **kwargs})

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _Runner)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality: {
            "degradation": True,
            "quality_before": baseline_quality,
            "quality_after": 90,
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("dgov.cli.run.emit_event", _capture_emit_event)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0

    # Find run_completed event
    run_completed_events = [e for e in captured_events if e.get("event") == "run_completed"]
    assert len(run_completed_events) == 1

    run_completed = run_completed_events[0]
    assert run_completed["pane"] == "compiled"
    assert run_completed["plan_name"] == "compiled"
    assert run_completed["run_status"] == "degraded"
    assert "duration_s" in run_completed
    assert isinstance(run_completed["duration_s"], float)
    assert run_completed["sentrux"] == {
        "degradation": True,
        "quality_before": 100,
        "quality_after": 90,
    }


def test_run_reports_structural_offenders_when_sentrux_degrades(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _write_plan_tree(tmp_path, "compiled")
    _write_compiled(plan_dir, "compiled")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @property
        def task_errors(self):
            return {}

        @property
        def task_durations(self):
            return {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

    monkeypatch.setattr("dgov.cli.run._compile_plan_for_run", lambda path: None)
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", _Runner)
    monkeypatch.setattr("dgov.cli.run._require_sentrux_baseline", lambda project_root: 100)
    monkeypatch.setattr(
        "dgov.cli.run._sentrux_compare",
        lambda project_root, baseline_quality: {
            "degradation": True,
            "quality_before": baseline_quality,
            "quality_after": 90,
            "structural_offenders": {
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
            },
        },
    )
    monkeypatch.setattr("dgov.cli.run._append_run_log", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Likely structural offenders at abc123" in result.output


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
