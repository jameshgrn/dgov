"""Tests for strict dgov run requirements (Plan Tree enforcement)."""

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


def test_run_uncompiled_plan_fails(runner: CliRunner, tmp_path: Path) -> None:
    """dgov run should fail if the plan is not compiled (missing source_mtime_max)."""
    plan = tmp_path / "plan.toml"
    plan.write_text(
        '[plan]\nname = "uncompiled"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )
    result = runner.invoke(cli, ["run", str(plan)])
    assert result.exit_code != 0
    assert "not compiled" in result.output.lower()
    assert "_root.toml" in result.output


def test_run_compiled_plan_passes_check(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    """dgov run should pass the check if source_mtime_max is present
    (sop_set_hash not required).
    """
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )

    # We mock the actual runner execution to just test the pre-run check
    # because full execution requires FIREWORKS_API_KEY and actual worktrees.
    monkeypatch.setattr("dgov.cli.run.EventDagRunner", lambda *args, **kwargs: None)

    # We'll likely hit an error later when it tries to use the None runner,
    # but the point is it passed the "uncompiled" check.
    result = runner.invoke(cli, ["run", str(plan)])

    # It should NOT fail with "not compiled"
    assert "not compiled" not in result.output.lower()


def test_run_auto_bootstraps_dgov_only_repo(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / ".dgov" / "plans" / "bootstrap" / "_compiled.toml"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            self._task_errors = {}
            self._task_durations = {}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

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

    try:
        result = runner.invoke(cli, ["run", str(plan)], catch_exceptions=False)
    finally:
        monkeypatch.undo()

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
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            self._task_errors = {"a": "boom"}
            self._task_durations = {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "failed"}

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

    result = runner.invoke(cli, ["run", str(plan)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "status: failed" in result.output
    assert "boom" in result.output


def test_run_auto_creates_bootstrap_commit_in_headless(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.cli.run._sentrux_available", lambda: True)

    # In headless (isatty=False), it should auto-create commit and then fail on missing baseline
    result = runner.invoke(cli, ["run", str(plan)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "created bootstrap commit from current working tree" in result.output.lower()
    assert "no sentrux baseline found" in result.output.lower()

    # Verify commit exists
    git_log = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "chore: bootstrap repo for dgov" in git_log


def test_run_requires_sentrux_baseline(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    monkeypatch.setattr("dgov.cli.run._sentrux_available", lambda: True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["run", str(plan)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "no sentrux baseline found" in result.output.lower()
    assert "dgov sentrux gate-save" in result.output


def test_run_fails_when_final_sentrux_compare_degrades(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "_compiled.toml"
    plan.write_text(
        '[plan]\nname = "compiled"\n'
        'source_mtime_max = "2026-04-08T00:00:00Z"\n\n'
        "[tasks.a]\n"
        'summary = "do a"\n'
        'prompt = "do a"\n'
        'commit_message = "a"\n'
    )

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            self._task_errors = {}
            self._task_durations = {"a": 0.1}

        async def run(self) -> dict[str, str]:
            return {"a": "merged"}

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

    result = runner.invoke(cli, ["run", str(plan)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "status: failed" in result.output
    assert "sentrux: architectural degradation detected." in result.output.lower()


def test_clean_head_worktree_isolates_from_dirty_state(tmp_path: Path) -> None:
    """_clean_head_worktree yields a checkout at HEAD, ignoring dirty working-tree changes.

    Regression for ledger bug #26: the post-run sentrux gate used to scan the
    live working tree, so uncommitted changes in the governor's workspace were
    falsely attributed to the run.
    """
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

    # Dirty the working tree after committing.
    tracked.write_text("x = 2  # uncommitted\n")
    (repo / "untracked.py").write_text("y = 999\n")

    with _clean_head_worktree(str(repo)) as scan_dir:
        # The scan dir reflects HEAD, not the live working tree.
        assert (scan_dir / "tracked.py").read_text() == "x = 1\n"
        assert not (scan_dir / "untracked.py").exists()
        snapshot = scan_dir

    # Cleanup removes the temporary worktree after the context exits.
    assert not snapshot.exists()
    # The main working tree is untouched.
    assert tracked.read_text() == "x = 2  # uncommitted\n"
    assert (repo / "untracked.py").exists()
