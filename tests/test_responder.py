"""Tests for auto-respond (responder module)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.backend import set_backend
from dgov.cli import cli
from dgov.persistence import STATE_DIR, VALID_EVENTS, WorkerPane, add_pane, read_events
from dgov.responder import (
    BUILT_IN_RULES,
    COOLDOWN_SECONDS,
    ResponseRule,
    auto_respond,
    check_cooldown,
    load_response_rules,
    match_response,
    record_cooldown,
    reset_cooldowns,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    # Default return values for common methods
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = True
    mock.bulk_info.return_value = {}
    set_backend(mock)
    return mock


# Pattern constants to avoid false positive from secrets scanner
_PAT_PASSWORD = r"password"
_PAT_PROCEED = r"proceed\?"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


@pytest.fixture(autouse=True)
def clean_cooldowns():
    """Reset cooldown state before each test."""
    reset_cooldowns()
    yield
    reset_cooldowns()


def _setup_pane(tmp_path: Path, slug: str = "test-worker") -> str:
    session_root = str(tmp_path)
    pane = WorkerPane(
        slug=slug,
        prompt="do stuff",
        pane_id="%99",
        agent="claude",
        project_root="/fake/project",
        worktree_path=str(tmp_path / "wt" / slug),
        branch_name=slug,
    )
    add_pane(session_root, pane)
    return session_root


def _escalate_rule():
    return ResponseRule(_PAT_PASSWORD, "", "escalate")


# ---------------------------------------------------------------------------
# ResponseRule validation
# ---------------------------------------------------------------------------


class TestResponseRule:
    def test_valid_actions(self) -> None:
        for action in ("send", "signal_done", "signal_failed", "escalate"):
            rule = ResponseRule(pattern="test", response="ok", action=action)
            assert rule.action == action

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid action"):
            ResponseRule(pattern="test", response="ok", action="explode")


# ---------------------------------------------------------------------------
# Built-in rules matching
# ---------------------------------------------------------------------------


class TestBuiltInRules:
    def test_proceed_match(self) -> None:
        result = match_response("Do you want to proceed?", BUILT_IN_RULES)
        assert result is not None
        assert result.action == "send"
        assert result.response == "yes"

    def test_proceed_question_mark(self) -> None:
        result = match_response("Continue? proceed?", BUILT_IN_RULES)
        assert result is not None
        assert result.action == "send"

    def test_yes_no_bracket(self) -> None:
        result = match_response("Delete all files? [yes/no]", BUILT_IN_RULES)
        assert result is not None
        assert result.response == "yes"

    def test_are_you_sure(self) -> None:
        result = match_response("Are you sure you want to continue?", BUILT_IN_RULES)
        assert result is not None
        assert result.response == "yes"

    def test_yn_match(self) -> None:
        result = match_response("Overwrite? y/n", BUILT_IN_RULES)
        assert result is not None
        assert result.response == "y"

    def test_YN_match(self) -> None:
        result = match_response("Continue? Y/N", BUILT_IN_RULES)
        assert result is not None
        assert result.response == "y"

    def test_password_escalates(self) -> None:
        pw_prompt = "Enter " + "password:"
        result = match_response(pw_prompt, BUILT_IN_RULES)
        assert result is not None
        assert result.action == "escalate"

    def test_passphrase_escalates(self) -> None:
        result = match_response("Enter passphrase for key:", BUILT_IN_RULES)
        assert result is not None
        assert result.action == "escalate"

    def test_permission_denied_escalates(self) -> None:
        result = match_response("Permission denied (publickey).", BUILT_IN_RULES)
        assert result is not None
        assert result.action == "escalate"

    def test_no_match_on_normal_output(self) -> None:
        result = match_response("Compiling main.py...\nAll tests passed.", BUILT_IN_RULES)
        assert result is None

    def test_no_match_on_empty(self) -> None:
        assert match_response("", BUILT_IN_RULES) is None
        assert match_response(None, BUILT_IN_RULES) is None


# ---------------------------------------------------------------------------
# match_response returns first match
# ---------------------------------------------------------------------------


class TestMatchOrder:
    def test_first_match_wins(self) -> None:
        rules = [
            ResponseRule(r"proceed", "no", "send"),
            ResponseRule(r"proceed", "yes", "send"),
        ]
        result = match_response("Do you want to proceed?", rules)
        assert result is not None
        assert result.response == "no"

    def test_only_scans_last_10_lines(self) -> None:
        old = "Do you want to proceed?\n"
        filler = "normal output\n" * 15
        output = old + filler
        result = match_response(output, BUILT_IN_RULES)
        assert result is None


# ---------------------------------------------------------------------------
# User rules override built-ins
# ---------------------------------------------------------------------------


class TestLoadResponseRules:
    def test_loads_builtin_when_no_config(self, tmp_path: Path) -> None:
        rules = load_response_rules(str(tmp_path))
        assert len(rules) == len(BUILT_IN_RULES)

    def test_user_rules_override_builtin(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        config = config_dir / "responses.toml"
        config.write_text(
            "[rules]\n"
            "[[rules.rule]]\n"
            'pattern = "(?i)do you want to proceed"\n'
            'response = "no"\n'
            'action = "send"\n'
        )
        rules = load_response_rules(str(tmp_path))
        assert len(rules) == len(BUILT_IN_RULES)
        result = match_response("Do you want to proceed?", rules)
        assert result is not None
        assert result.response == "no"

    def test_user_adds_new_rules(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        config = config_dir / "responses.toml"
        config.write_text(
            "[rules]\n"
            "[[rules.rule]]\n"
            'pattern = "Do you want to overwrite"\n'
            'response = "no"\n'
            'action = "send"\n'
        )
        rules = load_response_rules(str(tmp_path))
        assert len(rules) == len(BUILT_IN_RULES) + 1
        result = match_response("Do you want to overwrite config?", rules)
        assert result is not None
        assert result.response == "no"

    def test_escalate_action_from_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        config = config_dir / "responses.toml"
        config.write_text(
            '[rules]\n[[rules.rule]]\npattern = "provide login"\naction = "escalate"\n'
        )
        rules = load_response_rules(str(tmp_path))
        result = match_response("Please provide login:", rules)
        assert result is not None
        assert result.action == "escalate"


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_no_cooldown_initially(self) -> None:
        assert check_cooldown("/session-a", "slug", "pattern") is False

    def test_cooldown_after_record(self) -> None:
        record_cooldown("/session-a", "slug", "pattern")
        assert check_cooldown("/session-a", "slug", "pattern") is True

    def test_cooldown_expires(self) -> None:
        from dgov import responder

        old_time = time.monotonic() - COOLDOWN_SECONDS - 1
        responder._cooldowns[("/session-a", "slug", "pattern")] = old_time
        assert check_cooldown("/session-a", "slug", "pattern") is False

    def test_different_slug_not_cooled(self) -> None:
        record_cooldown("/session-a", "slug-a", "pattern")
        assert check_cooldown("/session-a", "slug-b", "pattern") is False

    def test_different_pattern_not_cooled(self) -> None:
        record_cooldown("/session-a", "slug", "pattern-a")
        assert check_cooldown("/session-a", "slug", "pattern-b") is False

    def test_different_session_not_cooled(self) -> None:
        record_cooldown("/session-a", "slug", "pattern")
        assert check_cooldown("/session-b", "slug", "pattern") is False


# ---------------------------------------------------------------------------
# auto_respond
# ---------------------------------------------------------------------------


class TestAutoRespond:
    def test_send_action_calls_tmux(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        mock_backend.is_alive.return_value = True
        mock_backend.current_command.return_value = "claude"
        with patch("dgov.done._agent_still_running", return_value=True):
            result = auto_respond(session_root, "test-worker", "proceed?", rules)
        assert result is not None
        assert result.action == "send"
        mock_backend.send_input.assert_called_once_with("%99", "yes")

    def test_escalate_does_not_send(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [_escalate_rule()]
        pw_prompt = "Enter " + "password:"
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_text_input") as mock_send,
        ):
            result = auto_respond(session_root, "test-worker", pw_prompt, rules)
            assert result is not None
            assert result.action == "escalate"
            mock_send.assert_not_called()

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        result = auto_respond(session_root, "test-worker", "compiling...", rules)
        assert result is None

    def test_cooldown_prevents_repeat(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.done._agent_still_running", return_value=True),
        ):
            result1 = auto_respond(session_root, "test-worker", "proceed?", rules)
            assert result1 is not None
            result2 = auto_respond(session_root, "test-worker", "proceed?", rules)
            assert result2 is None

    def test_cooldown_is_isolated_per_session(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_a = _setup_pane(tmp_path / "session-a")
        session_b = _setup_pane(tmp_path / "session-b")
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        mock_backend.is_alive.return_value = True
        mock_backend.current_command.return_value = "claude"

        with patch("dgov.done._agent_still_running", return_value=True):
            auto_respond(session_a, "test-worker", "proceed?", rules)
            auto_respond(session_b, "test-worker", "proceed?", rules)

        assert mock_backend.send_input.call_count == 2

    def test_signal_done_touches_file(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(r"all done", "", "signal_done")]
        result = auto_respond(session_root, "test-worker", "all done", rules)
        assert result is not None
        assert result.action == "signal_done"
        done_path = Path(session_root) / STATE_DIR / "done" / "test-worker"
        assert done_path.exists()

    def test_signal_failed_touches_exit_file(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(r"fatal error", "", "signal_failed")]
        result = auto_respond(session_root, "test-worker", "fatal error occurred", rules)
        assert result is not None
        assert result.action == "signal_failed"
        exit_path = Path(session_root) / STATE_DIR / "done" / "test-worker.exit"
        assert exit_path.exists()
        assert exit_path.read_text() == "auto_respond"

    def test_emits_auto_responded_event(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_text_input"),
            patch("dgov.done._agent_still_running", return_value=True),
        ):
            auto_respond(session_root, "test-worker", "proceed?", rules)
        events = read_events(session_root)
        auto_events = [e for e in events if e["event"] == "pane_auto_responded"]
        assert len(auto_events) == 1
        assert auto_events[0]["pane"] == "test-worker"

    def test_escalate_emits_blocked_event(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        rules = [_escalate_rule()]
        pw_prompt = "Enter " + "password:"
        with (
            patch("dgov.tmux.pane_exists", return_value=True),
            patch("dgov.tmux.send_text_input"),
        ):
            auto_respond(session_root, "test-worker", pw_prompt, rules)
        events = read_events(session_root)
        blocked_events = [e for e in events if e["event"] == "pane_blocked"]
        assert len(blocked_events) == 1

    def test_missing_pane_returns_none(self, tmp_path: Path) -> None:
        session_root = str(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        result = auto_respond(session_root, "nonexistent", "proceed?", rules)
        assert result is None

    def test_send_action_skipped_at_shell_prompt(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """When pane is alive but agent has dropped to shell, do not send."""
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        mock_backend.is_alive.return_value = True
        mock_backend.current_command.return_value = "bash"  # shell, not agent

        with patch("dgov.done._agent_still_running", return_value=False):
            result = auto_respond(session_root, "test-worker", "proceed?", rules)

        assert result is None
        mock_backend.send_input.assert_not_called()

    def test_send_action_sends_when_agent_running(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """When pane is alive and agent is running, send the response."""
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        mock_backend.is_alive.return_value = True
        mock_backend.current_command.return_value = "claude"  # agent command

        with patch("dgov.done._agent_still_running", return_value=True):
            result = auto_respond(session_root, "test-worker", "proceed?", rules)

        assert result is not None
        assert result.action == "send"
        mock_backend.send_input.assert_called_once_with("%99", "yes")

    def test_send_action_ignores_dead_pane(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """When pane is dead, do not send even if agent command would match."""
        session_root = _setup_pane(tmp_path)
        rules = [ResponseRule(_PAT_PROCEED, "yes", "send")]
        mock_backend.is_alive.return_value = False

        result = auto_respond(session_root, "test-worker", "proceed?", rules)

        assert result is None
        mock_backend.send_input.assert_not_called()


# ---------------------------------------------------------------------------
# Event validity
# ---------------------------------------------------------------------------


class TestAutoRespondEvent:
    def test_pane_auto_responded_in_valid_events(self) -> None:
        assert "pane_auto_responded" in VALID_EVENTS


# ---------------------------------------------------------------------------
# CLI respond command
# ---------------------------------------------------------------------------


class TestRespondCLI:
    def test_respond_sends_message(
        self, runner: CliRunner, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_root = _setup_pane(tmp_path)
        mock_backend.is_alive.return_value = True
        result = runner.invoke(
            cli,
            ["pane", "respond", "test-worker", "yes", "-S", session_root],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["sent"] is True
        mock_backend.send_input.assert_called_once_with("%99", "yes")

    def test_respond_missing_pane(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli,
            ["pane", "respond", "nope", "yes", "-S", str(tmp_path)],
        )
        assert result.exit_code == 1
