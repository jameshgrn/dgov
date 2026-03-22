"""Tests for governor-worker communication."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.done import _is_done
from dgov.persistence import STATE_DIR, VALID_EVENTS, get_pane
from dgov.waiter import (
    _detect_blocked,
    interact_with_pane,
    nudge_pane,
    signal_pane,
    wait_worker_pane,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


def _setup_pane(tmp_path: Path, slug: str = "test-worker", state: str = "active") -> str:
    """Create a pane record in the state DB and return session_root."""
    from dgov.persistence import WorkerPane, add_pane

    session_root = str(tmp_path)
    pane = WorkerPane(
        slug=slug,
        prompt="do stuff",
        pane_id="%99",
        agent="claude",
        project_root="/fake/project",
        worktree_path=str(tmp_path / "wt" / slug),
        branch_name=slug,
        state=state,
    )
    add_pane(session_root, pane)
    return session_root


# ---------------------------------------------------------------------------
# _detect_blocked
# ---------------------------------------------------------------------------


class TestDetectBlocked:
    def test_none_on_empty(self) -> None:
        assert _detect_blocked("") is None
        assert _detect_blocked(None) is None

    def test_matches_proceed(self) -> None:
        output = "Some output\nDo you want to proceed? (y/n)"
        result = _detect_blocked(output)
        assert result is not None
        assert "proceed" in result.lower()

    def test_matches_yn(self) -> None:
        assert _detect_blocked("Continue? y/n") is not None

    def test_matches_YN(self) -> None:
        assert _detect_blocked("Overwrite? Y/N") is not None

    def test_matches_yes_no_bracket(self) -> None:
        assert _detect_blocked("Delete files? [yes/no]") is not None

    def test_matches_are_you_sure(self) -> None:
        assert _detect_blocked("Are you sure you want to continue?") is not None

    def test_matches_password(self) -> None:
        assert _detect_blocked("Enter password:") is not None

    def test_matches_passphrase(self) -> None:
        assert _detect_blocked("Enter passphrase for key:") is not None

    def test_matches_permission_denied(self) -> None:
        assert _detect_blocked("Permission denied (publickey).") is not None

    def test_no_match_on_normal_output(self) -> None:
        assert _detect_blocked("Compiling main.py...\nAll tests passed.") is None

    def test_only_scans_last_10_lines(self) -> None:
        old_blocked = "Do you want to proceed?\n"
        filler = "normal output\n" * 15
        output = old_blocked + filler
        assert _detect_blocked(output) is None


# ---------------------------------------------------------------------------
# interact_with_pane
# ---------------------------------------------------------------------------


class TestInteract:
    def test_sends_message(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="claude"),
            patch("dgov.tmux.send_text_input") as mock_send,
        ):
            result = interact_with_pane(session_root, "test-worker", "hello agent")
            assert result is True
            mock_send.assert_called_once_with("%99", "hello agent")

    def test_returns_false_for_missing_pane(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        result = interact_with_pane(session_root, "nonexistent", "hello")
        assert result is False

    def test_returns_false_for_dead_pane(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with patch("dgov.tmux.pane_exists", return_value=False):
            result = interact_with_pane(session_root, "test-worker", "hello")
            assert result is False

    def test_returns_false_when_agent_has_fallen_back_to_shell(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="zsh"),
            patch("dgov.tmux.send_text_input") as mock_send,
        ):
            result = interact_with_pane(session_root, "test-worker", "hello")
        assert result is False
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# nudge_pane
# ---------------------------------------------------------------------------


class TestNudge:
    def test_parses_yes_response(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.done._has_completion_commit", return_value=True),
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="claude"),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.tmux.capture_pane", return_value="Are you done?\nYES"),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "YES"
            # Verify done-signal file was touched
            done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
            assert done_path.exists()

    def test_yes_response_without_commit_does_not_mark_done(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.done._has_completion_commit", return_value=False),
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="claude"),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.tmux.capture_pane", return_value="Are you done?\nYES"),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "YES"
            done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
            assert not done_path.exists()

    def test_parses_no_response(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="claude"),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.tmux.capture_pane", return_value="NO, still working."),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "NO"
            done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
            assert not done_path.exists()

    def test_unclear_when_no_match(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="claude"),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.tmux.capture_pane", return_value="I'm processing files..."),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "unclear"

    def test_error_for_missing_pane(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        result = nudge_pane(session_root, "nonexistent", wait_seconds=1)
        assert result["response"] == "error"

    def test_nudge_refuses_shell_fallback(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="zsh"),
            patch("dgov.tmux.send_text_input") as mock_send,
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
        assert result["response"] == "error"
        assert result["output"] == "Agent not attached"
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# signal_pane
# ---------------------------------------------------------------------------


class TestSignal:
    def test_signal_done(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with patch("dgov.done._has_completion_commit", return_value=True):
            result = signal_pane(session_root, "test-worker", "done")
        assert result is True
        done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
        assert done_path.exists()
        assert get_pane(session_root, "test-worker")["state"] == "done"

    def test_signal_done_requires_commit(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with patch("dgov.done._has_completion_commit", return_value=False):
            result = signal_pane(session_root, "test-worker", "done")
        assert result is False
        done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
        assert not done_path.exists()

    def test_signal_failed(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = signal_pane(session_root, "test-worker", "failed")
        assert result is True
        exit_path = Path(session_root) / STATE_DIR / "done" / "test-worker.exit"
        assert exit_path.exists()
        assert exit_path.read_text() == "manual"

    def test_late_signal_done_on_failed_pane_is_successful_noop(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, state="failed")
        with patch("dgov.done._has_completion_commit", return_value=True):
            result = signal_pane(session_root, "test-worker", "done")
        assert result is True
        done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
        assert done_path.exists()
        assert get_pane(session_root, "test-worker")["state"] == "failed"

    def test_returns_false_for_missing_pane(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        result = signal_pane(session_root, "nonexistent", "done")
        assert result is False

    def test_rejects_invalid_signal(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with pytest.raises(ValueError, match="Unknown signal"):
            signal_pane(session_root, "test-worker", "paused")


# ---------------------------------------------------------------------------
# pane_blocked event
# ---------------------------------------------------------------------------


class TestPaneBlockedEvent:
    def test_pane_blocked_in_valid_events(self) -> None:
        assert "pane_blocked" in VALID_EVENTS


class TestWaitWorkerPane:
    def test_wait_worker_pane_reloads_pane_state_each_poll(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        pane = get_pane(session_root, "test-worker")
        assert pane is not None

        poll_states: list[str] = []
        get_calls = {"count": 0}

        def fake_get_pane(_session_root: str, _slug: str) -> dict:
            get_calls["count"] += 1
            state = "active" if get_calls["count"] == 1 else "failed"
            return {**pane, "state": state}

        def fake_poll_once(
            _session_root: str,
            _project_root: str,
            _slug: str,
            pane_record: dict | None,
            _stable_state: dict,
            _stable: int,
            done_strategy=None,
            alive=None,
        ) -> tuple[bool, str]:
            poll_states.append(pane_record["state"] if pane_record else "")
            return (len(poll_states) > 1, "exit_signal" if len(poll_states) > 1 else "")

        with (
            patch("dgov.persistence.get_pane", side_effect=fake_get_pane),
            patch("dgov.waiter._poll_once", side_effect=fake_poll_once),
            patch("dgov.waiter._strategy_for_pane", return_value=None),
            patch("dgov.waiter.time.sleep"),
        ):
            result = wait_worker_pane(
                session_root,
                "test-worker",
                session_root=session_root,
                poll=0,
                auto_retry=False,
            )

        assert result == {"done": "test-worker", "method": "exit_signal"}
        assert poll_states == ["active", "failed"]


# ---------------------------------------------------------------------------
# unified _is_done with and without stabilization
# ---------------------------------------------------------------------------


class TestUnifiedIsDone:
    def test_done_signal_file(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        slug = "test-done"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()
        assert _is_done(session_root, slug) is False

    def test_exit_file_marks_failed(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        slug = "test-fail"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{slug}.exit").write_text("1")
        assert _is_done(session_root, slug) is True

    def test_no_signal_returns_false(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        slug = "test-active"
        _setup_pane(tmp_path, slug=slug)
        with patch("dgov.tmux.pane_exists", return_value=True):
            assert _is_done(session_root, slug) is False

    def test_without_stable_seconds_skips_stabilization(self, tmp_path: Path) -> None:
        """Without stable_seconds, _is_done does NOT do output stabilization."""
        session_root = str(tmp_path)
        slug = "test-nostable"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {"pane_id": "%99", "project_root": "", "branch_name": "", "base_sha": ""}
        with patch("dgov.tmux.pane_exists", return_value=True):
            result = _is_done(session_root, slug, pane_record=pane_record)
            assert result is False

    def test_with_stable_seconds_detects_stable(self, tmp_path: Path) -> None:
        """Stable output only completes a pane after a real commit exists."""
        from dgov.agents import DoneStrategy

        session_root = str(tmp_path)
        slug = "test-stable"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {
            "pane_id": "%99",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                alive=True,
                stable_seconds=5,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=5),
            )
            assert result is True

    def test_with_stable_seconds_agent_running_resets(self, tmp_path: Path) -> None:
        """If agent is still running, stabilization timer resets."""
        from dgov.agents import DoneStrategy

        session_root = str(tmp_path)
        slug = "test-agent-alive"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {"pane_id": "%99", "project_root": "", "branch_name": "", "base_sha": ""}

        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=True),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                alive=True,
                stable_seconds=5,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=5),
            )
            assert result is False
            assert stable_state["stable_since"] is None

    # ---------------------------------------------------------------------------
    # CLI commands
    # ---------------------------------------------------------------------------

    def test_message_cli_rejects_shell_fallback(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.current_command", return_value="zsh"),
        ):
            result = runner.invoke(
                cli,
                ["pane", "message", "test-worker", "hello", "-S", session_root],
            )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"] == "Pane test-worker agent not attached"


class TestSignalCLI:
    def test_signal_done_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with patch("dgov.done._has_completion_commit", return_value=True):
            result = runner.invoke(
                cli,
                ["pane", "signal", "test-worker", "done", "-S", session_root],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["signaled"] == "done"

    def test_signal_done_cli_requires_commit(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with patch("dgov.done._has_completion_commit", return_value=False):
            result = runner.invoke(
                cli,
                ["pane", "signal", "test-worker", "done", "-S", session_root],
            )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "completion commit" in data["error"]

    def test_signal_failed_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = runner.invoke(
            cli,
            ["pane", "signal", "test-worker", "failed", "-S", session_root],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["signaled"] == "failed"
