"""Tests for governor-worker communication."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import STATE_DIR, VALID_EVENTS
from dgov.waiter import (
    _detect_blocked,
    _is_done,
    interact_with_pane,
    nudge_pane,
    signal_pane,
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
            patch("dgov.tmux.send_command") as mock_send,
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


# ---------------------------------------------------------------------------
# nudge_pane
# ---------------------------------------------------------------------------


class TestNudge:
    def test_parses_yes_response(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_command"),
            patch("dgov.tmux.capture_pane", return_value="Are you done?\nYES"),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "YES"
            # Verify done-signal file was touched
            done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
            assert done_path.exists()

    def test_parses_no_response(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_command"),
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
            patch("dgov.tmux.send_command"),
            patch("dgov.tmux.capture_pane", return_value="I'm processing files..."),
            patch("dgov.waiter.time.sleep"),
        ):
            result = nudge_pane(session_root, "test-worker", wait_seconds=1)
            assert result["response"] == "unclear"

    def test_error_for_missing_pane(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        result = nudge_pane(session_root, "nonexistent", wait_seconds=1)
        assert result["response"] == "error"


# ---------------------------------------------------------------------------
# signal_pane
# ---------------------------------------------------------------------------


class TestSignal:
    def test_signal_done(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = signal_pane(session_root, "test-worker", "done")
        assert result is True
        done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
        assert done_path.exists()

    def test_signal_failed(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = signal_pane(session_root, "test-worker", "failed")
        assert result is True
        exit_path = Path(session_root) / STATE_DIR / "done" / "test-worker.exit"
        assert exit_path.exists()
        assert exit_path.read_text() == "manual"

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
        assert _is_done(session_root, slug) is True

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
        """With stable_seconds and matching output, _is_done returns True when stabilized."""
        session_root = str(tmp_path)
        slug = "test-stable"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {"pane_id": "%99", "project_root": "", "branch_name": "", "base_sha": ""}

        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.panes.capture_worker_output", return_value="same output"),
            patch("dgov.waiter._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                stable_seconds=5,
                _stable_state=stable_state,
            )
            assert result is True

    def test_with_stable_seconds_agent_running_resets(self, tmp_path: Path) -> None:
        """If agent is still running, stabilization timer resets."""
        session_root = str(tmp_path)
        slug = "test-agent-alive"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {"pane_id": "%99", "project_root": "", "branch_name": "", "base_sha": ""}

        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.panes.capture_worker_output", return_value="same output"),
            patch("dgov.waiter._agent_still_running", return_value=True),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                stable_seconds=5,
                _stable_state=stable_state,
            )
            assert result is False
            assert stable_state["stable_since"] is None


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


class TestRespondCLI:
    def test_respond_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_command"),
        ):
            result = runner.invoke(
                cli,
                ["pane", "respond", "test-worker", "hello", "-S", session_root],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["sent"] is True

    def test_respond_cli_missing(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli,
            ["pane", "respond", "nope", "hello", "-S", str(tmp_path)],
        )
        assert result.exit_code == 1


class TestSignalCLI:
    def test_signal_done_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = runner.invoke(
            cli,
            ["pane", "signal", "test-worker", "done", "-S", session_root],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["signaled"] == "done"

    def test_signal_failed_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        result = runner.invoke(
            cli,
            ["pane", "signal", "test-worker", "failed", "-S", session_root],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["signaled"] == "failed"


class TestNudgeCLI:
    def test_nudge_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_command"),
            patch("dgov.tmux.capture_pane", return_value="YES I'm done"),
            patch("dgov.waiter.time.sleep"),
        ):
            result = runner.invoke(
                cli,
                ["pane", "nudge", "test-worker", "-S", session_root, "-w", "1"],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["response"] == "YES"
