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
from dgov.cli import _check_governor_context, cli

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

        monkeypatch.setattr("dgov.cli.subprocess.run", fake_run)
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
            patch("dgov.tmux.style_dgov_session") as mock_style_session,
            patch("dgov.tmux.style_governor_pane") as mock_style_governor,
            patch(
                "dgov.cli.subprocess.run",
                return_value=_cp(stdout="%11\n"),
            ),
        ):
            result = runner.invoke(cli, [])

        assert result.exit_code == 0
        assert f"{tmp_path.name} \u2014 governor ready" in result.output
        mock_style_session.assert_called_once_with()
        mock_style_governor.assert_called_once_with("%11")

    def test_outside_tmux_creates_session_and_attaches(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TMUX", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            calls.append(cmd)
            if cmd[1:3] == ["has-session", "-t"]:
                return _cp(returncode=1)
            return _cp()

        with (
            patch("dgov.cli.subprocess.run", side_effect=fake_run),
            patch("dgov.tmux.style_dgov_session") as mock_style_session,
            patch("dgov.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(cli, [])

        assert result.exit_code == 0
        session_name = f"dgov-{tmp_path.name}"
        assert ["tmux", "has-session", "-t", session_name] in calls
        assert ["tmux", "new-session", "-d", "-s", session_name] in calls
        mock_style_session.assert_called_once_with(session_name)
        mock_execvp.assert_called_once_with(
            "tmux",
            ["tmux", "attach-session", "-t", session_name],
        )


class TestPaneHelp:
    def test_pane_help_lists_current_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["pane", "--help"])
        assert result.exit_code == 0
        for command in (
            "util",
            "create",
            "close",
            "merge",
            "wait",
            "wait-all",
            "merge-all",
            "list",
            "prune",
            "classify",
            "capture",
            "review",
            "diff",
            "escalate",
            "retry",
        ):
            assert command in result.output


class TestUtilityPanes:
    @pytest.mark.parametrize(
        ("argv", "command", "title"),
        [
            (["pane", "lazygit"], "lazygit", "lazygit"),
            (["pane", "yazi"], "yazi", "yazi"),
            (["pane", "htop"], "htop", "htop"),
            (["pane", "k9s"], "k9s", "k9s"),
            (["pane", "top"], "btop", "btop"),
        ],
    )
    def test_shortcuts_launch_utility_pane(
        self, runner: CliRunner, argv: list[str], command: str, title: str
    ) -> None:
        with patch("dgov.tmux.create_utility_pane", return_value="%22") as mock_create:
            result = runner.invoke(cli, argv)

        assert result.exit_code == 0
        mock_create.assert_called_once_with(command, f"[util] {title}", cwd=".")
        assert json.loads(result.output) == {
            "pane_id": "%22",
            "command": command,
            "title": title,
        }

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
            "dgov.panes.create_worker_pane",
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
        assert mock_create.call_args.kwargs == {
            "project_root": "/repo",
            "prompt": "Fix tests",
            "agent": "claude",
            "permission_mode": "acceptEdits",
            "slug": None,
            "env_vars": {"FOO": "bar", "BAZ": "qux"},
            "extra_flags": "--verbose",
            "session_root": "/session",
            "skip_auto_structure": False,
        }

    def test_auto_classifies_prompt(self, runner: CliRunner) -> None:
        with (
            patch("dgov.panes.classify_task", return_value="claude") as mock_classify,
            patch("dgov.panes.create_worker_pane", return_value=_pane("auto-task", "claude")),
        ):
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "auto", "--prompt", "Fix lint", "--no-preflight"],
            )

        assert result.exit_code == 0
        mock_classify.assert_called_once_with("Fix lint")
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
            patch("dgov.panes.create_worker_pane", return_value=_pane("fixed-task")),
        ):
            result = runner.invoke(
                cli,
                ["pane", "create", "--agent", "claude", "--prompt", "Fix lint"],
            )

        assert result.exit_code == 0
        mock_fix.assert_called_once_with(failing, ".")


class TestPaneCommands:
    def test_close_success_and_not_found(self, runner: CliRunner) -> None:
        with patch("dgov.panes.close_worker_pane", return_value=True):
            ok = runner.invoke(cli, ["pane", "close", "task"])
        with patch("dgov.panes.close_worker_pane", return_value=False):
            missing = runner.invoke(cli, ["pane", "close", "missing"])

        assert ok.exit_code == 0
        assert json.loads(ok.output) == {"closed": "task"}
        assert missing.exit_code == 1
        assert json.loads(missing.output)["error"] == "Pane not found: missing"

    def test_merge_uses_selected_strategy(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"merged": "task", "branch": "task"},
        ) as mock_merge_close:
            close_result = runner.invoke(cli, ["pane", "merge", "task"])

        with patch(
            "dgov.panes.merge_worker_pane",
            return_value={"merged": "task", "branch": "task"},
        ) as mock_merge:
            open_result = runner.invoke(
                cli,
                ["pane", "merge", "task", "--no-close", "--resolve", "manual"],
            )

        assert close_result.exit_code == 0
        assert open_result.exit_code == 0
        mock_merge_close.assert_called_once_with(".", "task", session_root=None, resolve="agent")
        mock_merge.assert_called_once_with(".", "task", session_root=None, resolve="manual")

    def test_merge_error_exits_nonzero(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.merge_worker_pane_with_close",
            return_value={"error": "conflicts"},
        ):
            result = runner.invoke(cli, ["pane", "merge", "task"])

        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "conflicts"

    def test_wait_success_and_timeout(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.wait_worker_pane",
            return_value={"done": "task", "method": "stable"},
        ):
            ok = runner.invoke(cli, ["pane", "wait", "task"])

        timeout = __import__("dgov.panes", fromlist=["PaneTimeoutError"]).PaneTimeoutError(
            "task",
            30,
            "pi",
        )
        with patch("dgov.panes.wait_worker_pane", side_effect=timeout):
            failed = runner.invoke(cli, ["pane", "wait", "task"])

        assert ok.exit_code == 0
        assert json.loads(ok.output) == {"done": "task", "method": "stable"}
        assert failed.exit_code == 1
        assert json.loads(failed.output) == {
            "error": "Timeout after 30s",
            "slug": "task",
            "agent": "pi",
            "suggest_escalate": True,
        }

    def test_wait_all_handles_empty_and_timeout(self, runner: CliRunner) -> None:
        with patch("dgov.panes.list_worker_panes", return_value=[]):
            empty = runner.invoke(cli, ["pane", "wait-all"])

        timeout = __import__("dgov.panes", fromlist=["PaneTimeoutError"]).PaneTimeoutError(
            "a",
            10,
            "pi",
            pending_panes=[{"slug": "a", "agent": "pi"}, {"slug": "b", "agent": "claude"}],
        )
        with (
            patch(
                "dgov.panes.list_worker_panes",
                return_value=[{"slug": "a", "done": False}, {"slug": "b", "done": False}],
            ),
            patch("dgov.panes.wait_all_worker_panes", side_effect=timeout),
        ):
            failed = runner.invoke(cli, ["pane", "wait-all"])

        assert empty.exit_code == 0
        assert json.loads(empty.output) == {"done": "all", "count": 0}
        assert failed.exit_code == 1
        lines = [json.loads(line) for line in failed.output.strip().splitlines()]
        assert lines == [
            {
                "error": "Timeout after 10s",
                "slug": "a",
                "agent": "pi",
                "suggest_escalate": True,
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
            patch("dgov.panes.list_worker_panes", return_value=panes),
            patch(
                "dgov.panes.merge_worker_pane_with_close",
                side_effect=[
                    {"merged": "a", "files_changed": 2},
                    {"error": "conflict"},
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
            "closed": ["a"],
            "failed": ["b"],
            "warnings": ["b: conflict"],
        }

    def test_list_prune_classify_and_capture(self, runner: CliRunner) -> None:
        with patch("dgov.panes.list_worker_panes", return_value=[{"slug": "task"}]):
            listed = runner.invoke(cli, ["pane", "list", "--json"])
        with patch("dgov.panes.prune_stale_panes", return_value=["old-task"]):
            pruned = runner.invoke(cli, ["pane", "prune"])
        with patch("dgov.panes.classify_task", return_value="claude"):
            classified = runner.invoke(cli, ["pane", "classify", "debug flaky test"])
        with patch("dgov.panes.capture_worker_output", return_value="line 1\nline 2"):
            captured = runner.invoke(cli, ["pane", "capture", "task", "--lines", "50"])

        assert json.loads(listed.output) == [{"slug": "task"}]
        assert json.loads(pruned.output) == {"pruned": ["old-task"]}
        assert json.loads(classified.output) == {
            "recommended_agent": "claude",
            "prompt_preview": "debug flaky test",
        }
        assert captured.exit_code == 0
        assert captured.output == "line 1\nline 2\n"

    def test_capture_missing_exits(self, runner: CliRunner) -> None:
        with patch("dgov.panes.capture_worker_output", return_value=None):
            result = runner.invoke(cli, ["pane", "capture", "missing"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"error": "Pane not found or dead: missing"}

    def test_review_diff_escalate_and_retry(self, runner: CliRunner) -> None:
        with patch(
            "dgov.panes.review_worker_pane",
            return_value={"slug": "task", "verdict": "safe"},
        ):
            review = runner.invoke(cli, ["pane", "review", "task", "--full"])
        with patch(
            "dgov.panes.diff_worker_pane",
            return_value={"slug": "task", "diff": "patch"},
        ):
            diff = runner.invoke(cli, ["pane", "diff", "task", "--name-only"])
        with patch(
            "dgov.panes.escalate_worker_pane",
            return_value={"escalated": True, "agent": "claude"},
        ):
            escalate = runner.invoke(
                cli,
                ["pane", "escalate", "task", "--agent", "claude", "--permission-mode", "plan"],
            )
        with patch(
            "dgov.panes.retry_worker_pane",
            return_value={"retried": True, "new_slug": "task-2"},
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
        assert json.loads(escalate.output)["escalated"] is True
        assert json.loads(retry.output)["retried"] is True


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
            "dgov.state.get_status",
            return_value={"panes": [], "tunnel": {"any_up": False}},
        ):
            status = runner.invoke(cli, ["status"])
        with patch(
            "dgov.panes.rebase_governor",
            return_value={"rebased": True, "base": "main"},
        ) as mock_rebase:
            rebase = runner.invoke(cli, ["rebase", "--project-root", "/repo", "--onto", "develop"])
        with patch("dgov.cli.detect_installed_agents", return_value=["claude"]):
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
        assert json.loads(status.output)["tunnel"]["any_up"] is False
        mock_rebase.assert_called_once_with("/repo", onto="develop")
        assert json.loads(rebase.output)["rebased"] is True
        agents_payload = json.loads(agents.output)
        assert next(item for item in agents_payload if item["id"] == "claude")["installed"] is True
        assert json.loads(version.output) == {"dgov": __version__}

    def test_checkpoint_and_batch_commands(self, runner: CliRunner, tmp_path: Path) -> None:
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps({"project_root": "/repo", "tasks": []}))

        with patch(
            "dgov.panes.create_checkpoint",
            return_value={"checkpoint": "wave-1", "main_sha": "abc", "pane_count": 1},
        ):
            created = runner.invoke(cli, ["checkpoint", "create", "wave-1"])
        with patch(
            "dgov.panes.list_checkpoints",
            return_value=[{"name": "wave-1", "pane_count": 1}],
        ) as mock_list_checkpoints:
            listed = runner.invoke(cli, ["checkpoint", "list", "--project-root", "/repo"])
        with patch(
            "dgov.panes.run_batch",
            return_value={"dry_run": True, "tiers": [["a"]], "total_tasks": 1},
        ) as mock_run_batch:
            batch_ok = runner.invoke(cli, ["batch", str(spec_path), "--dry-run"])
        with patch(
            "dgov.panes.run_batch",
            return_value={"failed": ["a"], "tiers": []},
        ):
            batch_fail = runner.invoke(cli, ["batch", str(spec_path)])

        assert created.exit_code == 0
        assert json.loads(created.output)["checkpoint"] == "wave-1"
        assert listed.exit_code == 0
        mock_list_checkpoints.assert_called_once_with(str(Path("/repo").resolve()))
        assert json.loads(listed.output) == [{"name": "wave-1", "pane_count": 1}]
        mock_run_batch.assert_called_once_with(str(spec_path), session_root=None, dry_run=True)
        assert json.loads(batch_ok.output)["dry_run"] is True
        assert batch_fail.exit_code == 1
        assert json.loads(batch_fail.output)["failed"] == ["a"]
