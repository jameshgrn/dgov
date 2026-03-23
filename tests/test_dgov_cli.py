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
from dgov.executor import PaneFinalizeResult

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
            "wait-all",
            "merge-all",
            "land-all",
            "list",
            "gc",
            "classify",
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
    def test_create_success_passes_env_vars(self, runner: CliRunner) -> None:
        with patch(
            "dgov.lifecycle.create_worker_pane",
            return_value=_pane("lint-fix", "claude"),
        ) as mock_create:
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
                    "--extra-flags",
                    "--verbose",
                    "--env",
                    "FOO=bar",
                    "--env",
                    "BAZ=qux",
                    "--no-preflight",
                ],
            )

        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "slug": "lint-fix",
            "pane_id": "%7",
            "agent": "claude",
            "worktree": "/tmp/lint-fix",
            "branch": "lint-fix",
        }
        kwargs = mock_create.call_args.kwargs
        assert kwargs["project_root"] == "/repo"
        assert kwargs["prompt"] == "Fix tests"
        assert kwargs["agent"] == "claude"
        assert kwargs["permission_mode"] == "bypassPermissions"
        assert kwargs["slug"] is None
        assert kwargs["env_vars"] == {"FOO": "bar", "BAZ": "qux"}
        assert kwargs["extra_flags"] == "--verbose"
        assert kwargs["session_root"] == "/session"
        assert kwargs["skip_auto_structure"] is False
        assert kwargs["role"] == "worker"
        assert kwargs["parent_slug"] == ""
        assert kwargs["context_packet"].prompt == "Fix tests"

    def test_auto_classifies_prompt(self, runner: CliRunner) -> None:
        with (
            patch("dgov.strategy.classify_task", return_value="claude") as mock_classify,
            patch("dgov.lifecycle.create_worker_pane", return_value=_pane("auto-task", "claude")),
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

        with (
            patch("dgov.preflight.run_preflight", return_value=failing),
            patch("dgov.preflight.fix_preflight", return_value=fixed) as mock_fix,
            patch("dgov.lifecycle.create_worker_pane", return_value=_pane("fixed-task")),
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

        with (
            patch("dgov.strategy.extract_task_context") as mock_context,
            patch("dgov.preflight.run_preflight", return_value=report) as mock_preflight,
            patch("dgov.lifecycle.create_worker_pane", return_value=_pane("merge-fix")),
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
                "src/dgov/inspection.py",
            ],
            expected_branch=None,
            session_root=None,
            skip_deps=True,
        )

    def test_pane_create_explicit_touches_override_prompt_inference(
        self, runner: CliRunner
    ) -> None:
        report = MagicMock()
        report.passed = True

        with (
            patch("dgov.strategy.extract_task_context") as mock_context,
            patch("dgov.preflight.run_preflight", return_value=report) as mock_preflight,
            patch(
                "dgov.lifecycle.create_worker_pane",
                return_value=_pane("touch-fix"),
            ) as mock_create,
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
        )
        packet = mock_create.call_args.kwargs["context_packet"]
        assert packet.file_claims == ("src/dgov/cli/pane.py", "tests/test_dgov_cli.py")
        assert packet.edit_files == ("src/dgov/cli/pane.py", "tests/test_dgov_cli.py")


class TestPaneCommands:
    def test_close_success_and_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.lifecycle.close_worker_pane", return_value=True):
            ok = runner.invoke(cli, ["pane", "close", "task"])
        with patch("dgov.lifecycle.close_worker_pane", return_value=False):
            missing = runner.invoke(cli, ["pane", "close", "missing"])

        assert ok.exit_code == 0
        assert json.loads(ok.output) == {"closed": "task"}
        assert missing.exit_code == 1
        assert json.loads(missing.output) == {"not_found": "missing"}

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

    def test_wait_all_handles_empty_and_timeout(self, runner: CliRunner) -> None:
        with patch("dgov.status.list_worker_panes", return_value=[]):
            empty = runner.invoke(cli, ["pane", "wait-all"])

        timeout = __import__("dgov.waiter", fromlist=["PaneTimeoutError"]).PaneTimeoutError(
            "a",
            10,
            "pi",
            pending_panes=[{"slug": "a", "agent": "pi"}, {"slug": "b", "agent": "claude"}],
        )
        with (
            patch(
                "dgov.status.list_worker_panes",
                return_value=[{"slug": "a", "done": False}, {"slug": "b", "done": False}],
            ),
            patch("dgov.waiter.wait_all_worker_panes", side_effect=timeout),
        ):
            failed = runner.invoke(cli, ["pane", "wait-all"])

        assert empty.exit_code == 0
        assert json.loads(empty.output) == {"done": "all", "count": 0}
        assert failed.exit_code == 1
        lines = [json.loads(line) for line in failed.output.strip().splitlines()]
        # Neither pi nor claude are in ESCALATION_CHAIN, so suggest_escalate is False
        assert lines == [
            {
                "error": "Timeout after 10s",
                "slug": "a",
                "agent": "pi",
            },
            {
                "error": "Timeout after 10s",
                "slug": "b",
                "agent": "claude",
            },
        ]

    def test_merge_all_summarizes_results(self, runner: CliRunner) -> None:
        panes = [
            {"slug": "a", "done": True},
            {"slug": "b", "done": True},
            {"slug": "c", "done": False},
        ]
        with (
            patch("dgov.status.list_worker_panes", return_value=panes),
            patch(
                "dgov.executor.run_finalize_panes",
                return_value=[
                    PaneFinalizeResult(
                        slug="a",
                        review={"slug": "a", "verdict": "safe", "commit_count": 1},
                        merge_result={"merged": "a", "files_changed": 2},
                        error=None,
                        cleanup_error=None,
                    ),
                    PaneFinalizeResult(
                        slug="b",
                        review={"slug": "b", "verdict": "safe", "commit_count": 1},
                        merge_result=None,
                        error="conflict",
                        cleanup_error=None,
                    ),
                ],
            ),
        ):
            result = runner.invoke(cli, ["pane", "merge-all"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "merged_count": 1,
            "failed_count": 1,
            "total_files_changed": 2,
            "merged": ["a"],
            "failed": ["b"],
            "warnings": ["b: conflict"],
        }

    def test_merge_all_passes_selected_strategy(self, runner: CliRunner) -> None:
        panes = [{"slug": "a", "done": True}]

        with (
            patch("dgov.status.list_worker_panes", return_value=panes),
            patch(
                "dgov.executor.run_finalize_panes",
                return_value=[
                    PaneFinalizeResult(
                        slug="a",
                        review={"slug": "a", "verdict": "safe", "commit_count": 1},
                        merge_result={"merged": "a", "branch": "a"},
                        error=None,
                        cleanup_error=None,
                    )
                ],
            ) as mock_merge,
        ):
            result = runner.invoke(cli, ["pane", "merge-all", "--resolve", "manual"])

        assert result.exit_code == 0
        mock_merge.assert_called_once_with(
            ".", ["a"], session_root=None, resolve="manual", squash=True, rebase=False, close=False
        )

    def test_merge_all_blocks_non_safe_review(self, runner: CliRunner) -> None:
        panes = [{"slug": "a", "done": True}]

        with (
            patch("dgov.status.list_worker_panes", return_value=panes),
            patch(
                "dgov.executor.run_finalize_panes",
                return_value=[
                    PaneFinalizeResult(
                        slug="a",
                        review={"slug": "a", "verdict": "review", "commit_count": 1},
                        merge_result=None,
                        error="Review verdict is review; refusing to merge",
                        cleanup_error=None,
                    )
                ],
            ) as mock_merge,
        ):
            result = runner.invoke(cli, ["pane", "merge-all"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "merged_count": 0,
            "failed_count": 1,
            "total_files_changed": 0,
            "merged": [],
            "failed": ["a"],
            "warnings": ["a: review verdict is review; refusing to merge"],
        }
        mock_merge.assert_called_once_with(
            ".", ["a"], session_root=None, resolve="skip", squash=True, rebase=False, close=False
        )

    def test_pane_batch_blocks_preflight_failure(self, runner: CliRunner, tmp_path: Path) -> None:
        spec_path = tmp_path / "pane-batch.toml"
        spec_path.write_text(
            """
[tasks.fix-parser]
agent = "claude"
prompt = "Fix parser bug in src/parser.py"
"""
        )
        report = MagicMock()
        report.passed = False

        with (
            patch("dgov.preflight.run_preflight", return_value=report),
            patch("dgov.lifecycle.create_worker_pane") as mock_create,
        ):
            result = runner.invoke(cli, ["pane", "batch", str(spec_path)])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "dispatched": 0,
            "failed": 1,
            "panes": [],
            "errors": [{"slug": "fix-parser", "error": "preflight failed"}],
        }
        mock_create.assert_not_called()

    def test_pane_batch_uses_declared_touches(self, runner: CliRunner, tmp_path: Path) -> None:
        spec_path = tmp_path / "pane-batch.toml"
        spec_path.write_text(
            """
[tasks.fix-parser]
agent = "claude"
prompt = "Fix parser bug in src/parser.py"
touches = ["src/parser.py", "tests/test_parser.py"]
"""
        )
        report = MagicMock()
        report.passed = True
        created: dict[str, object] = {}

        def fake_create_worker_pane(**kwargs):  # noqa: ANN003, ANN201
            created.update(kwargs)
            return _pane("fix-parser")

        with (
            patch("dgov.preflight.run_preflight", return_value=report),
            patch("dgov.lifecycle.create_worker_pane", side_effect=fake_create_worker_pane),
        ):
            result = runner.invoke(cli, ["pane", "batch", str(spec_path)])

        assert result.exit_code == 0
        packet = created["context_packet"]
        assert packet.file_claims == ("src/parser.py", "tests/test_parser.py")

    def test_land_all_summarizes_results(self, runner: CliRunner) -> None:
        panes = [
            {"slug": "a", "done": True},
            {"slug": "b", "done": True},
            {"slug": "c", "done": False},
        ]
        with (
            patch("dgov.status.list_worker_panes", return_value=panes),
            patch(
                "dgov.executor.run_finalize_panes",
                return_value=[
                    PaneFinalizeResult(
                        slug="a",
                        review={"slug": "a", "verdict": "safe", "commit_count": 1},
                        merge_result={"merged": "a", "files_changed": 2},
                        error=None,
                        cleanup_error=None,
                    ),
                    PaneFinalizeResult(
                        slug="b",
                        review={"slug": "b", "verdict": "safe", "commit_count": 0},
                        merge_result=None,
                        error="No commits to merge",
                        cleanup_error=None,
                    ),
                ],
            ) as mock_land,
        ):
            result = runner.invoke(cli, ["pane", "land-all"])

        assert result.exit_code == 1
        lines = result.output.strip().splitlines()
        assert json.loads(lines[0]) == {"review": "safe", "commits": 1, "slug": "a"}
        assert json.loads(lines[1]) == {"review": "safe", "commits": 0, "slug": "b"}
        assert json.loads("\n".join(lines[2:])) == {
            "landed_count": 1,
            "failed_count": 1,
            "total_files_changed": 2,
            "landed": ["a"],
            "failed": ["b"],
            "warnings": ["b: no commits to merge"],
        }
        assert mock_land.call_count == 1

    def test_land_all_skips_non_safe_review(self, runner: CliRunner) -> None:
        panes = [{"slug": "a", "done": True}]
        with (
            patch("dgov.status.list_worker_panes", return_value=panes),
            patch(
                "dgov.executor.run_finalize_panes",
                return_value=[
                    PaneFinalizeResult(
                        slug="a",
                        review={"slug": "a", "verdict": "review", "commit_count": 1},
                        merge_result=None,
                        error="Review verdict is review; refusing to merge",
                        cleanup_error=None,
                    ),
                ],
            ) as mock_land,
        ):
            result = runner.invoke(cli, ["pane", "land-all"])

        assert result.exit_code == 1
        lines = result.output.strip().splitlines()
        assert json.loads(lines[0]) == {"review": "review", "commits": 1, "slug": "a"}
        assert json.loads("\n".join(lines[1:])) == {
            "landed_count": 0,
            "failed_count": 1,
            "total_files_changed": 0,
            "landed": [],
            "failed": ["a"],
            "warnings": ["a: review verdict is review; refusing to merge"],
        }
        mock_land.assert_called_once_with(
            ".",
            ["a"],
            session_root=None,
            resolve="skip",
            squash=True,
            rebase=False,
            close=True,
        )

    def test_list_gc_and_classify(self, runner: CliRunner) -> None:
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
        with patch("dgov.strategy.classify_task", return_value="claude"):
            classified = runner.invoke(cli, ["pane", "classify", "debug flaky test"])

        assert json.loads(listed.output) == [{"slug": "task"}]
        assert json.loads(gc.output) == {
            "closed": ["old-review"],
            "skipped": ["recent-failed"],
            "pruned": ["old-task"],
        }
        assert json.loads(classified.output) == {
            "recommended_agent": "claude",
            "prompt_preview": "debug flaky test",
        }

    def test_review_diff_escalate_and_retry(self, runner: CliRunner) -> None:
        with patch(
            "dgov.executor.run_review_only",
            return_value=MagicMock(review={"slug": "task", "verdict": "safe"}),
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
            agents = runner.invoke(cli, ["agents"])
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


class TestDagCliCommand:
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
