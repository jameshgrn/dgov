"""Unit tests for dgov.cli."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dgov import __version__
from dgov.cli import _check_governor_context, _ensure_governor_session, cli
from dgov.inspection import ReviewInfo

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


def _pane(slug: str = "task", agent: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        pane_id="%7",
        agent=agent,
        worktree_path=f"/tmp/{slug}",
        branch_name=slug,
    )


def _cp(*, stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


class TestGovernorContext:
    def test_skip_env_short_circuits_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            calls.append(cmd)
            return _cp()

        monkeypatch.setattr("dgov.cli.subprocess.run", fake_run)
        _check_governor_context()
        assert calls == []

    def test_rejects_running_inside_worktree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DGOV_SKIP_GOVERNOR_CHECK", raising=False)
        monkeypatch.setattr(
            "dgov.cli.subprocess.run",
            lambda cmd, **kwargs: _cp(stdout=".git/worktrees/test-audit\n"),
        )
        with pytest.raises(click.UsageError, match="main repo"):
            _check_governor_context()

    def test_rejects_non_main_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DGOV_SKIP_GOVERNOR_CHECK", raising=False)

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            if cmd[-1] == "--git-dir":
                return _cp(stdout=".git\n")
            return _cp(stdout="feature/test\n")

        with patch("dgov.cli.subprocess.run", side_effect=fake_run):
            with pytest.raises(click.UsageError, match="must stay on 'main'"):
                _check_governor_context()

    def test_ignores_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DGOV_SKIP_GOVERNOR_CHECK", raising=False)

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            raise TimeoutError

        def fake_subprocess_run(cmd: list[str], **kwargs) -> MagicMock:
            raise __import__("subprocess").TimeoutExpired(cmd, 5)

        monkeypatch.setattr("dgov.cli.subprocess.run", fake_subprocess_run)
        _check_governor_context()


class TestBareCli:
    def test_inside_tmux_styles_governor_pane(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMUX", "1")

        with (
            patch("dgov.tmux.setup_governor_workspace"),
            patch("dgov.tmux.style_dgov_session") as mock_style_session,
            patch("dgov.tmux.style_governor_pane") as mock_style_governor,
            patch(
                "dgov.cli.subprocess.run",
                return_value=_cp(stdout="%11\n"),
            ),
            patch("dgov.agents.get_governor_agent", return_value=("claude", "")),
            patch("dgov.agents.write_project_config"),
            patch("dgov.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(cli, [])

        assert result.exit_code == 0
        assert f"{tmp_path.name} \u2014 governor ready" in result.output
        mock_style_session.assert_called_once_with()
        mock_style_governor.assert_called_once_with("%11")
        mock_execvp.assert_called_once()

    def test_outside_tmux_creates_session_and_attaches(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TMUX", raising=False)

        with (
            patch("dgov.cli._ensure_governor_session", return_value=(True, True)) as mock_ensure,
            patch("dgov.cli.os.execvp") as mock_execvp,
            patch("dgov.agents.get_governor_agent", return_value=("claude", "plan")),
            patch("dgov.agents.write_project_config") as mock_write,
        ):
            result = runner.invoke(cli, [])

        assert result.exit_code == 0
        session_name = f"dgov-{tmp_path.name}"
        mock_ensure.assert_called_once_with(
            str(tmp_path),
            session_name,
            "claude",
            "bypassPermissions",
        )
        mock_write.assert_any_call(str(tmp_path), "governor_permissions", "bypassPermissions")
        mock_execvp.assert_called_once_with(
            "tmux",
            ["tmux", "attach-session", "-t", session_name],
        )


class TestEnsureGovernorSession:
    def test_skips_duplicate_launch_when_governor_is_already_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            calls.append(cmd)
            if cmd[0:2] == ["tmux", "list-panes"]:
                return _cp(stdout="%1|[gov] main|codex\n%2|[gov] terrain|python\n")
            if cmd[0:2] == ["tmux", "send-keys"]:
                return _cp()
            return _cp()

        registry = {"codex": SimpleNamespace(prompt_command="codex")}
        monkeypatch.setattr("dgov.cli._ensure_tmux_session", lambda *args: False)
        monkeypatch.setattr("dgov.cli.subprocess.run", fake_run)

        with (
            patch("dgov.tmux.setup_governor_workspace"),
            patch("dgov.tmux.style_dgov_session"),
            patch("dgov.tmux.style_governor_pane") as mock_style_governor,
            patch("dgov.agents.load_registry", return_value=registry),
            patch("dgov.agents.build_launch_command", return_value="codex --dangerously-bypass"),
        ):
            created, started = _ensure_governor_session(
                str(tmp_path),
                "dgov-test",
                "codex",
                "bypassPermissions",
            )

        assert created is False
        assert started is False
        mock_style_governor.assert_called_once_with("%1")
        assert not any(cmd[0:2] == ["tmux", "send-keys"] for cmd in calls)

    def test_launches_governor_into_first_pane_when_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            calls.append(cmd)
            if cmd[0:2] == ["tmux", "list-panes"]:
                return _cp(stdout="%9||zsh\n%10|[gov] terrain|python\n")
            return _cp()

        registry = {"gemini": SimpleNamespace(prompt_command="gemini")}
        monkeypatch.setattr("dgov.cli._ensure_tmux_session", lambda *args: True)
        monkeypatch.setattr("dgov.cli.subprocess.run", fake_run)

        with (
            patch("dgov.tmux.setup_governor_workspace"),
            patch("dgov.tmux.style_dgov_session"),
            patch("dgov.tmux.style_governor_pane") as mock_style_governor,
            patch("dgov.agents.load_registry", return_value=registry),
            patch("dgov.agents.build_launch_command", return_value="gemini --approval-mode yolo"),
        ):
            created, started = _ensure_governor_session(
                str(tmp_path),
                "dgov-test",
                "gemini",
                "bypassPermissions",
            )

        assert created is True
        assert started is True
        mock_style_governor.assert_called_once_with("%9")
        assert ["tmux", "send-keys", "-t", "%9", "gemini --approval-mode yolo", "Enter"] in calls


class TestResumeAndRefresh:
    def test_resume_recreates_missing_session_from_saved_config(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TMUX", raising=False)

        with (
            patch("dgov.agents.get_governor_agent", return_value=("gemini", "plan")),
            patch("dgov.agents.write_project_config") as mock_write,
            patch("dgov.cli._ensure_governor_session", return_value=(True, True)) as mock_ensure,
            patch("dgov.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(cli, ["resume"])

        assert result.exit_code == 0
        mock_write.assert_any_call(str(tmp_path), "governor_permissions", "bypassPermissions")
        mock_ensure.assert_called_once_with(
            str(tmp_path),
            f"dgov-{tmp_path.name}",
            "gemini",
            "bypassPermissions",
        )
        mock_execvp.assert_called_once_with(
            "tmux",
            ["tmux", "attach-session", "-t", f"dgov-{tmp_path.name}"],
        )

    def test_refresh_rebuilds_missing_session_instead_of_exiting(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TMUX", raising=False)
        (tmp_path / ".dgov").mkdir()

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            if cmd[:3] == ["uv", "tool", "install"]:
                return _cp()
            return _cp()

        with (
            patch("dgov.agents.get_governor_agent", return_value=("codex", "")),
            patch("dgov.agents.write_project_config") as mock_write,
            patch("dgov.cli.subprocess.run", side_effect=fake_run),
            patch("dgov.cli._ensure_governor_session", return_value=(True, True)) as mock_ensure,
            patch("dgov.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(cli, ["refresh", "-r", str(tmp_path)])

        assert result.exit_code == 0
        mock_write.assert_any_call(str(tmp_path), "governor_permissions", "bypassPermissions")
        mock_ensure.assert_called_once_with(
            str(tmp_path),
            f"dgov-{tmp_path.name}",
            "codex",
            "bypassPermissions",
        )
        mock_execvp.assert_called_once_with(
            "tmux",
            ["tmux", "attach-session", "-t", f"dgov-{tmp_path.name}"],
        )


class TestPlanCommands:
    def test_plan_scratch_writes_under_dgov_plans(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["plan", "scratch", "review_refactor"])

        expected = tmp_path / ".dgov" / "plans" / "review_refactor.toml"
        assert result.exit_code == 0
        assert result.output.strip() == str(expected.resolve())
        assert expected.exists()

    def test_plan_scratch_refuses_overwrite_without_force(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)

        first = runner.invoke(cli, ["plan", "scratch", "river_test"])
        second = runner.invoke(cli, ["plan", "scratch", "river_test"])

        assert first.exit_code == 0
        assert second.exit_code == 1
        assert "already exists" in second.output


class TestPaneHelp:
    def test_pane_help_lists_current_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "--help"])
        assert result.exit_code == 0
        for command in (
            "util",
            "create",
            "close",
            "merge",
            "land",
            "wait",
            "list",
            "gc",
            "review",
            "diff",
            "escalate",
            "retry",
        ):
            assert command in result.output


class TestUtilityPanes:
    def test_util_allows_custom_title_and_cwd(self, runner: CliRunner) -> None:
        with patch("dgov.tmux.create_utility_pane", return_value="%30") as mock_create:
            result = runner.invoke(
                cli,
                ["pane", "util", "yazi /tmp", "--title", "files", "--cwd", "/repo"],
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with("yazi /tmp", "[util] files", cwd="/repo")
        assert json.loads(result.output) == {
            "pane_id": "%30",
            "command": "yazi /tmp",
            "title": "files",
        }


class TestPaneCreate:
    def _mock_plan_pipeline(self):
        """Mock the plan pipeline for worker dispatch tests."""
        mock_result = MagicMock(run_id=1, status="submitted")
        return (
            patch("dgov.plan.build_adhoc_plan", return_value=MagicMock(name="test")),
            patch("dgov.plan.write_adhoc_plan", return_value="/tmp/plan.toml"),
            patch("dgov.plan.run_plan", return_value=mock_result),
        )

    def test_create_success_via_plan(self, runner: CliRunner) -> None:
        m1, m2, m3 = self._mock_plan_pipeline()
        with m1 as mock_build, m2, m3:
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "--agent",
                    "claude",
                    "--prompt",
                    "Fix tests",
                    "--project-root",
                    "/repo",
                    "--session-root",
                    "/session",
                    "--no-preflight",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "dag_run_id" in output
        assert output["status"] == "submitted"
        kwargs = mock_build.call_args.kwargs
        assert kwargs["prompt"] == "Fix tests"
        assert kwargs["agent"] == "claude"

    def test_auto_classifies_prompt(self, runner: CliRunner) -> None:
        m1, m2, m3 = self._mock_plan_pipeline()
        with (
            patch("dgov.strategy.classify_task", return_value="claude") as mock_classify,
            m1,
            m2,
            m3,
        ):
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "auto", "--prompt", "Fix lint", "--no-preflight"],
            )

        assert result.exit_code == 0
        mock_classify.assert_called_once()
        assert mock_classify.call_args[0] == ("Fix lint",)
        assert json.loads(result.stderr)["auto_classified"] == "claude"

    def test_invalid_env_var_exits(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            [
                "pane",
                "create",
                "--agent",
                "claude",
                "--prompt",
                "Fix lint",
                "--env",
                "BROKEN",
                "--no-preflight",
            ],
        )
        assert result.exit_code == 1
        assert "KEY=VALUE" in result.output

    def test_unknown_agent_exits(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            ["pane", "create", "--agent", "unknown", "--prompt", "Fix lint", "--no-preflight"],
        )
        assert result.exit_code == 1
        assert "Unknown agent" in result.output

    def test_preflight_failure_exits_with_report(self, runner: CliRunner) -> None:
        report = MagicMock()
        report.passed = False
        report.to_dict.return_value = {"passed": False, "checks": [{"name": "git_clean"}]}

        with patch("dgov.preflight.run_preflight", return_value=report):
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "claude", "--prompt", "Fix lint", "--no-fix"],
            )

        assert result.exit_code == 1
        assert json.loads(result.output)["passed"] is False

    def test_preflight_fix_retries_report(self, runner: CliRunner) -> None:
        failing = MagicMock()
        failing.passed = False
        fixed = MagicMock()
        fixed.passed = True
        fixed.to_dict.return_value = {"passed": True}

        m1, m2, m3 = self._mock_plan_pipeline()
        with (
            patch("dgov.preflight.run_preflight", return_value=failing),
            patch("dgov.preflight.fix_preflight", return_value=fixed) as mock_fix,
            m1,
            m2,
            m3,
        ):
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "claude", "--prompt", "Fix lint"],
            )

        assert result.exit_code == 0
        mock_fix.assert_called_once_with(failing, ".")

    def test_preflight_uses_prompt_derived_touches(self, runner: CliRunner) -> None:
        report = MagicMock()
        report.passed = True

        m1, m2, m3 = self._mock_plan_pipeline()
        with (
            patch("dgov.strategy.extract_task_context") as mock_context,
            patch("dgov.preflight.run_preflight", return_value=report) as mock_preflight,
            m1,
            m2,
            m3,
        ):
            mock_context.return_value = {
                "primary_files": ["src/dgov/merger.py"],
                "also_check": ["src/dgov/inspection.py"],
                "tests": ["tests/test_merger_coverage.py"],
                "hints": [],
            }
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "claude", "--prompt", "Fix merge boundary bug"],
            )

        assert result.exit_code == 0
        mock_preflight.assert_called_once_with(
            project_root=".",
            agent="claude",
            touches=[
                "src/dgov/merger.py",
            ],
            expected_branch=None,
            session_root=None,
            skip_deps=True,
            derived_only=True,
        )

    def test_pane_create_explicit_touches_override_prompt_inference(
        self, runner: CliRunner
    ) -> None:
        report = MagicMock()
        report.passed = True

        m1, m2, m3 = self._mock_plan_pipeline()
        with (
            patch("dgov.strategy.extract_task_context") as mock_context,
            patch("dgov.preflight.run_preflight", return_value=report) as mock_preflight,
            m1 as mock_build,
            m2,
            m3,
        ):
            mock_context.return_value = {
                "primary_files": ["src/dgov/merger.py"],
                "also_check": ["src/dgov/inspection.py"],
                "tests": ["tests/test_merger_coverage.py"],
                "hints": [],
            }
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "--agent",
                    "claude",
                    "--prompt",
                    "Fix merge boundary bug",
                    "--touches",
                    "src/dgov/cli/pane.py",
                    "--touches",
                    "tests/test_dgov_cli.py",
                ],
            )

        assert result.exit_code == 0
        mock_preflight.assert_called_once_with(
            project_root=".",
            agent="claude",
            touches=["src/dgov/cli/pane.py", "tests/test_dgov_cli.py"],
            expected_branch=None,
            session_root=None,
            skip_deps=True,
            derived_only=False,
        )
        kwargs = mock_build.call_args.kwargs
        assert "src/dgov/cli/pane.py" in kwargs["touches"]
        assert "tests/test_dgov_cli.py" in kwargs["touches"]


class TestPaneCommands:
    def test_close_success_and_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.lifecycle.close_worker_pane", return_value=True):
            ok = runner.invoke(cli, ["pane", "close", "task"])
        with patch("dgov.lifecycle.close_worker_pane", return_value=False):
            missing = runner.invoke(cli, ["pane", "close", "missing"])

        assert ok.exit_code == 0
        assert json.loads(ok.output) == {"closed": "task"}
        assert missing.exit_code == 1
        out = json.loads(missing.output)
        assert "error" in out
        assert "missing" in out["error"]

    def test_wait_success_and_timeout(self, runner: CliRunner) -> None:
        with (
            patch(
                "dgov.executor.run_wait_only",
                return_value=MagicMock(
                    state="completed",
                    slug="task",
                    wait_result={"done": "task", "method": "stable"},
                    pane_state="done",
                ),
            ),
            patch(
                "dgov.status.list_worker_panes",
                return_value=[{"slug": "task", "done": False, "agent": "pi"}],
            ),
        ):
            ok = runner.invoke(cli, ["pane", "wait", "task"])

        with (
            patch(
                "dgov.executor.run_wait_only",
                return_value=MagicMock(
                    state="failed",
                    slug="task",
                    wait_result=None,
                    pane_state=None,
                    error="Worker timed out after 30s (retries exhausted)",
                    failure_stage="timeout",
                ),
            ),
            patch(
                "dgov.status.list_worker_panes",
                return_value=[{"slug": "task", "done": False, "agent": "pi"}],
            ),
            patch(
                "dgov.persistence.get_pane",
                return_value={"agent": "pi"},
            ),
        ):
            failed = runner.invoke(cli, ["pane", "wait", "task"])

        assert ok.exit_code == 0
        assert json.loads(ok.output) == {"done": "task", "method": "stable"}
        assert failed.exit_code == 1
        assert json.loads(failed.output) == {
            "error": "Worker timed out after 30s (retries exhausted)",
            "slug": "task",
            "suggest_escalate": True,
        }

    def test_pane_batch_blocks_preflight_failure(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec_path = tmp_path / "pane-batch.toml"
        spec_path.write_text(
            """
[tasks.fix-parser]
agent = "claude"
prompt = "Fix parser bug in src/parser.py"
"""
        )
        monkeypatch.chdir(tmp_path)
        summary = {
            "status": "failed",
            "tiers": [{"tier": 0, "tasks": [{"id": "fix-parser", "status": "failed"}]}],
            "merged": [],
            "failed": ["fix-parser"],
            "skipped": [],
            "blocked": [],
        }

        with patch("dgov.batch.run_batch", return_value=summary) as mock_run:
            result = runner.invoke(cli, ["pane", "batch", str(spec_path)])

        assert result.exit_code == 1
        assert json.loads(result.output) == summary
        mock_run.assert_called_once_with(
            str(spec_path),
            session_root=None,
            dry_run=False,
            project_root=".",
        )

    def test_pane_batch_uses_declared_touches(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec_path = tmp_path / "pane-batch.toml"
        spec_path.write_text(
            """
[tasks.fix-parser]
agent = "claude"
prompt = "Fix parser bug in src/parser.py"
touches = ["src/parser.py", "tests/test_parser.py"]
"""
        )
        monkeypatch.chdir(tmp_path)
        summary = {
            "status": "completed",
            "tiers": [
                {
                    "tier": 0,
                    "tasks": [
                        {
                            "id": "fix-parser",
                            "status": "merged",
                            "slug": "r1-fix-parser",
                        }
                    ],
                }
            ],
            "merged": ["fix-parser"],
            "failed": [],
            "skipped": [],
            "blocked": [],
        }

        with patch("dgov.batch.run_batch", return_value=summary) as mock_run:
            result = runner.invoke(cli, ["pane", "batch", str(spec_path)])

        assert result.exit_code == 0
        assert json.loads(result.output) == summary
        mock_run.assert_called_once_with(
            str(spec_path),
            session_root=None,
            dry_run=False,
            project_root=".",
        )

    def test_list_and_gc(self, runner: CliRunner) -> None:
        with patch("dgov.status.list_worker_panes", return_value=[{"slug": "task"}]):
            listed = runner.invoke(cli, ["pane", "list", "--json"])
        with (
            patch("dgov.status.prune_stale_panes", return_value=["old-task"]),
            patch(
                "dgov.status.gc_retained_panes",
                return_value={"closed": ["old-review"], "skipped": ["recent-failed"]},
            ),
        ):
            gc = runner.invoke(
                cli,
                ["pane", "gc", "--older-than-hours", "12", "--state", "review_pending"],
            )

        assert json.loads(listed.output) == [{"slug": "task"}]
        assert json.loads(gc.output) == {
            "closed": ["old-review"],
            "skipped": ["recent-failed"],
            "pruned": ["old-task"],
        }

    def test_review_diff_escalate_and_retry(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(review=ReviewInfo(slug="task", verdict="safe")),
        ):
            review = runner.invoke(cli, ["pane", "review", "task", "--full"])
        with patch(
            "dgov.inspection.diff_worker_pane",
            return_value={"slug": "task", "diff": "patch"},
        ):
            diff = runner.invoke(cli, ["pane", "diff", "task", "--name-only"])
        with patch(
            "dgov.executor.run_escalate_only",
            return_value=MagicMock(error=None, new_slug="task-esc", target_agent="claude"),
        ):
            escalate = runner.invoke(
                cli,
                ["pane", "escalate", "task", "--agent", "claude", "--permission-mode", "plan"],
            )
        with patch(
            "dgov.executor.run_retry_only",
            return_value=MagicMock(error=None, new_slug="task-2"),
        ):
            retry = runner.invoke(
                cli,
                ["pane", "retry", "task", "--agent", "pi", "--prompt", "Try again"],
            )

        assert review.exit_code == 0
        assert diff.exit_code == 0
        assert escalate.exit_code == 0
        assert retry.exit_code == 0
        assert json.loads(review.output)["verdict"] == "safe"
        assert json.loads(diff.output)["diff"] == "patch"
        assert json.loads(escalate.output)["new_slug"] == "task-esc"
        assert json.loads(retry.output)["new_slug"] == "task-2"


class TestTopLevelCommands:
    def test_preflight_status_rebase_agents_and_version(self, runner: CliRunner) -> None:
        report = MagicMock()
        report.passed = True
        report.to_dict.return_value = {"passed": True, "checks": []}

        with patch("dgov.preflight.run_preflight", return_value=report) as mock_preflight:
            preflight = runner.invoke(
                cli,
                [
                    "pane",
                    "preflight",
                    "--project-root",
                    "/repo",
                    "--session-root",
                    "/session",
                    "--agent",
                    "pi",
                    "--touches",
                    "src/app.py",
                    "--branch",
                    "main",
                ],
            )
        with patch(
            "dgov.status.list_worker_panes",
            return_value=[],
        ):
            status = runner.invoke(cli, ["status", "--json"])
        with patch(
            "dgov.inspection.rebase_governor",
            return_value={"rebased": True, "base": "main"},
        ) as mock_rebase:
            rebase = runner.invoke(cli, ["rebase", "--project-root", "/repo", "--onto", "develop"])
        with patch("dgov.cli.admin.detect_installed_agents", return_value=["claude"]):
            agents = runner.invoke(cli, ["agent", "list"])
        version = runner.invoke(cli, ["version"])

        assert preflight.exit_code == 0
        mock_preflight.assert_called_once_with(
            project_root="/repo",
            agent="pi",
            touches=["src/app.py"],
            expected_branch="main",
            session_root="/session",
        )
        assert json.loads(preflight.output)["passed"] is True
        assert json.loads(status.output)["total"] == 0
        mock_rebase.assert_called_once_with("/repo", onto="develop")
        assert json.loads(rebase.output)["rebased"] is True
        agents_payload = json.loads(agents.output)
        assert next(item for item in agents_payload if item["id"] == "claude")["installed"] is True
        assert json.loads(version.output) == {"dgov": __version__}

    def test_checkpoint_and_batch_commands(self, runner: CliRunner, tmp_path: Path) -> None:
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps({"project_root": "/repo", "tasks": []}))

        with patch(
            "dgov.batch.create_checkpoint",
            return_value={"checkpoint": "wave-1", "main_sha": "abc", "pane_count": 1},
        ):
            created = runner.invoke(cli, ["checkpoint", "create", "wave-1"])
        with patch(
            "dgov.batch.list_checkpoints",
            return_value=[{"name": "wave-1", "pane_count": 1}],
        ) as mock_list_checkpoints:
            listed = runner.invoke(cli, ["checkpoint", "list", "--project-root", "/repo"])
        with patch(
            "dgov.batch.run_batch",
            return_value={"dry_run": True, "tiers": [["a"]], "total_tasks": 1},
        ) as mock_run_batch:
            batch_ok = runner.invoke(cli, ["batch", str(spec_path), "--dry-run"])
        with patch(
            "dgov.batch.run_batch",
            return_value={"failed": ["a"], "tiers": []},
        ):
            batch_fail = runner.invoke(cli, ["batch", str(spec_path)])

        assert created.exit_code == 0
        assert json.loads(created.output)["checkpoint"] == "wave-1"
        assert listed.exit_code == 0
        mock_list_checkpoints.assert_called_once_with(str(Path("/repo").resolve()))
        assert json.loads(listed.output) == [{"name": "wave-1", "pane_count": 1}]
        mock_run_batch.assert_called_once_with(
            str(spec_path), project_root=".", session_root=None, dry_run=True
        )
        assert json.loads(batch_ok.output)["dry_run"] is True
        assert batch_fail.exit_code == 1
        assert json.loads(batch_fail.output)["failed"] == ["a"]

    def test_root_version_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.strip() == __version__

    def test_merge_queue_process_blocks_non_safe_review(self, runner: CliRunner) -> None:
        with (
            patch(
                "dgov.persistence.claim_next_merge",
                return_value={"branch": "task", "ticket": 7},
            ),
            patch("dgov.persistence.complete_merge") as mock_complete,
            patch("dgov.persistence.emit_event") as mock_emit,
            patch(
                "dgov.executor.run_land_only",
                return_value=MagicMock(
                    review={"slug": "task", "verdict": "review", "commit_count": 1},
                    merge_result=None,
                    error="Review verdict is review; refusing to merge",
                ),
            ) as mock_land,
        ):
            result = runner.invoke(cli, ["merge-queue", "process", "-r", "."])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ticket"] == 7
        assert payload["slug"] == "task"
        assert "refusing to merge" in payload["result"]["error"]
        mock_land.assert_called_once()
        mock_complete.assert_called_once()
        mock_emit.assert_not_called()


class TestDagWaitCommand:
    """Regression tests for dgov wait event payload handling."""

    def test_wait_top_level_event_payload(self, runner: CliRunner) -> None:
        """dgov wait succeeds on dag_completed with top-level dag_run_id/status and no data key."""
        # Simulate a flattened event (no 'data' key) - the new contract from wait_for_events()
        top_level_event = {
            "id": 42,
            "ts": "2026-03-30T12:00:00Z",
            "event": "dag_completed",
            "pane": "test-run",
            "dag_run_id": "run-123",
            "status": "completed",
            # Note: no 'data' key - this is the flattened contract
        }

        with (
            patch("dgov.persistence.latest_event_id", return_value=0),
            patch("dgov.persistence.wait_for_events", return_value=[top_level_event]),
        ):
            result = runner.invoke(cli, ["wait"])

        assert result.exit_code == 0
        assert "DAG COMPLETED" in result.output
        assert "run-123" in result.output

    def test_wait_interrupts_top_level_event_payload(self, runner: CliRunner) -> None:
        """dgov wait --interrupts prints blocked-task context from top-level event fields."""
        # Simulate a flattened dag_blocked event with top-level fields
        blocked_event = {
            "id": 43,
            "ts": "2026-03-30T12:01:00Z",
            "event": "dag_blocked",
            "pane": "test-run",
            "task": "fix-parser",
            "reason": "Preflight failed: merge conflicts detected",
            "report_path": "/tmp/report.json",
            # Note: no 'data' key - this is the flattened contract
        }

        # Create a mock report file
        report_content = json.dumps(
            {
                "role": "worker",
                "log_tail": "Error: cannot merge",
                "diff": "diff --git a/file.py",
            }
        )

        with (
            patch("dgov.persistence.latest_event_id", return_value=0),
            patch("dgov.persistence.wait_for_events", return_value=[blocked_event]),
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.read_text", return_value=report_content),
        ):
            result = runner.invoke(cli, ["wait", "--interrupts"])

        assert result.exit_code == 0
        assert "[INTERRUPT]" in result.output
        assert "fix-parser" in result.output
        assert "Preflight failed" in result.output
        assert "worker" in result.output  # from report
        assert "cannot merge" in result.output  # from log_tail

    """Tests for dgov dag CLI."""

    def test_dag_run_dry_run(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(p), "--dry-run"])
        assert result.exit_code == 0
        assert "Tier 0" in result.output

    def test_dag_run_skip_repeated(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
            [tasks.T1]
            summary = "Test2"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T1.files]
            create = ["b.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["dag", "run", str(p), "--dry-run", "--skip", "T0", "--skip", "T1"]
        )
        assert result.exit_code == 0

    def test_dag_merge_no_run(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "merge", str(p)])
        assert result.exit_code != 0  # no awaiting_merge run exists

    def test_dag_run_uses_project_root_option(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")

        captured = {}

        def mock_run_dag(*args, **kwargs):
            captured["project_root"] = kwargs.get("project_root")
            captured["session_root"] = kwargs.get("session_root")
            from dgov.dag import DagRunSummary

            return DagRunSummary(run_id=1, dag_file=str(p), status="submitted")

        monkeypatch.setattr("dgov.dag.run_dag", mock_run_dag)
        monkeypatch.setattr("dgov.monitor.ensure_monitor_running", lambda *a, **k: None)

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(p), "--project-root", "/custom/root"])
        assert result.exit_code == 0
        assert captured.get("project_root") == "/custom/root"
        assert captured.get("session_root") is None

    def test_dag_run_uses_dgov_project_root_env(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        monkeypatch.setenv("DGOV_PROJECT_ROOT", "/env/root")

        worktree_path = tmp_path / ".dgov" / "worktrees" / "test-123"
        worktree_path.mkdir(parents=True)
        dag_path = worktree_path / "dag.toml"
        dag_path.write_text(toml_content)

        captured = {}

        def mock_run_dag(*args, **kwargs):
            captured["project_root"] = kwargs.get("project_root")
            captured["session_root"] = kwargs.get("session_root")
            from dgov.dag import DagRunSummary

            return DagRunSummary(run_id=1, dag_file=str(dag_path), status="submitted")

        monkeypatch.setattr("dgov.dag.run_dag", mock_run_dag)
        monkeypatch.setattr("dgov.monitor.ensure_monitor_running", lambda *a, **k: None)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["dag", "run", str(dag_path), "--project-root", str(worktree_path)]
        )
        assert result.exit_code == 0
        assert captured.get("project_root") == "/env/root"
        assert captured.get("session_root") is None

    def test_dag_merge_uses_project_root_option(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")

        captured = {}

        def mock_merge_dag(*args, **kwargs):
            captured["project_root"] = kwargs.get("project_root")
            captured["session_root"] = kwargs.get("session_root")
            from dgov.dag import DagRunSummary

            return DagRunSummary(run_id=1, dag_file=str(p), status="completed", merged=["T0"])

        monkeypatch.setattr("dgov.dag.merge_dag", mock_merge_dag)

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "merge", str(p), "--project-root", "/custom/merge"])
        assert result.exit_code == 0
        assert captured.get("project_root") == "/custom/merge"
        assert captured.get("session_root") is None

    def test_dag_resume_passes_autocorrected_roots_to_run_dag(self, monkeypatch, tmp_path):
        import textwrap

        toml_content = textwrap.dedent(
            """\
            [dag]
            version = 1
            name = "test"
            [tasks.T0]
            summary = "Test"
            agent = "hunter"
            prompt = "do it"
            commit_message = "c"
            [tasks.T0.files]
            create = ["a.py"]
        """
        )
        p = tmp_path / "test.toml"
        p.write_text(toml_content)
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        monkeypatch.setattr("dgov.persistence.ensure_dag_tables", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda *args, **kwargs: {"id": 4, "status": "failed", "dag_file": str(p)},
        )
        monkeypatch.setattr("dgov.persistence.list_dag_tasks", lambda *args, **kwargs: [])
        monkeypatch.setattr("dgov.executor.run_resume_dag", lambda *args, **kwargs: None)

        captured = {}

        def mock_run_dag(*args, **kwargs):
            captured["project_root"] = kwargs.get("project_root")
            captured["session_root"] = kwargs.get("session_root")
            from dgov.dag import DagRunSummary

            return DagRunSummary(run_id=5, dag_file=str(p), status="submitted")

        monkeypatch.setattr("dgov.dag.run_dag", mock_run_dag)

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "resume", str(p), "--run-id", "4", "-r", "/repo"])
        assert result.exit_code == 0
        assert captured == {"project_root": "/repo", "session_root": "/repo"}
