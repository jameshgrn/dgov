"""Unit tests for dgov.tmux."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from dgov.tmux import (
    _run,
    capture_pane,
    create_utility_pane,
    current_command,
    kill_pane,
    pane_exists,
    select_layout,
    send_command,
    send_prompt_via_buffer,
    set_title,
    setup_pane_borders,
    split_pane,
    style_dgov_session,
    style_governor_pane,
    style_worker_pane,
)

pytestmark = pytest.mark.unit


def _cp(*, stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


class TestRun:
    def test_returns_stripped_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.tmux.subprocess.run",
            lambda *args, **kwargs: _cp(stdout=" %1 \n"),
        )
        assert _run(["list-sessions"]) == "%1"

    def test_raises_for_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.tmux.subprocess.run",
            lambda *args, **kwargs: _cp(returncode=1, stderr="no server"),
        )
        with pytest.raises(RuntimeError, match="no server"):
            _run(["list-sessions"])

    def test_silent_mode_suppresses_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.tmux.subprocess.run",
            lambda *args, **kwargs: _cp(returncode=1, stderr="ignored"),
        )
        assert _run(["list-sessions"], silent=True) == ""


class TestPaneCommands:
    def test_split_send_set_title_capture_and_select(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            seen.append(cmd)
            if "split-window" in cmd:
                return _cp(stdout="%5\n")
            if "capture-pane" in cmd:
                return _cp(stdout="line1\nline2\n")
            return _cp(stdout="zsh\n")

        monkeypatch.setattr("dgov.tmux.subprocess.run", fake_run)

        assert (
            split_pane(
                cwd="/repo",
                target="%1",
                env={"DISABLE_AUTO_UPDATE": "true", "DISABLE_UPDATE_PROMPT": "true"},
            )
            == "%5"
        )
        send_command("%5", "ls -la")
        set_title("%5", "worker")
        assert capture_pane("%5", lines=2) == "line1\nline2"
        assert current_command("%5") == "zsh"
        select_layout("tiled")
        kill_pane("%5")

        assert [
            "tmux",
            "split-window",
            "-h",
            "-P",
            "-F",
            "#{pane_id}",
            "-e",
            "DISABLE_AUTO_UPDATE=true",
            "-e",
            "DISABLE_UPDATE_PROMPT=true",
            "-t",
            "%1",
            "-c",
            "/repo",
        ] in seen
        assert ["tmux", "send-keys", "-t", "%5", "ls -la", "Enter"] in seen
        assert ["tmux", "select-pane", "-t", "%5", "-T", "worker"] in seen
        assert ["tmux", "select-layout", "tiled"] in seen
        assert ["tmux", "kill-pane", "-t", "%5"] in seen

    def test_pane_exists_checks_display_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.tmux._run", lambda args, silent=False: "%5")
        assert pane_exists("%5") is True
        monkeypatch.setattr("dgov.tmux._run", lambda args, silent=False: "")
        assert pane_exists("%5") is False

    def test_bulk_pane_info_parses_all_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.tmux import bulk_pane_info

        monkeypatch.setattr(
            "dgov.tmux._run",
            lambda args, silent=False: "%1|worker|claude\n%2|gov|zsh\n",
        )
        assert bulk_pane_info() == {
            "%1": {"title": "worker", "current_command": "claude"},
            "%2": {"title": "gov", "current_command": "zsh"},
        }

    def test_bulk_pane_info_returns_empty_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.tmux import bulk_pane_info

        def raise_err(args, silent=False):
            raise RuntimeError("no server")

        monkeypatch.setattr("dgov.tmux._run", raise_err)
        assert bulk_pane_info() == {}

    def test_bulk_pane_info_skips_malformed_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.tmux import bulk_pane_info

        monkeypatch.setattr(
            "dgov.tmux._run",
            lambda args, silent=False: "%1|worker|claude\nbadline\n\n%2|gov|zsh\n",
        )
        assert bulk_pane_info() == {
            "%1": {"title": "worker", "current_command": "claude"},
            "%2": {"title": "gov", "current_command": "zsh"},
        }


class TestStyling:
    def setup_method(self) -> None:
        # Clear the per-session cache so each test fires the tmux calls
        import dgov.tmux as _tmux_mod

        _tmux_mod._borders_configured.clear()

    def test_setup_pane_borders_applies_session_scope(self) -> None:
        with patch("dgov.tmux._run") as mock_run:
            setup_pane_borders("dgov-repo")

        # Single compound call (two set-option ops joined with ";")
        border_fmt = " #[bold]#P #[default]#{?pane_title,#{pane_title},#{pane_current_command}} "
        mock_run.assert_called_once_with(
            [
                "set-option",
                "-t",
                "dgov-repo",
                "pane-border-status",
                "top",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "pane-border-format",
                border_fmt,
            ],
            silent=True,
        )

    def test_setup_pane_borders_cached_on_second_call(self) -> None:
        with patch("dgov.tmux._run") as mock_run:
            setup_pane_borders("dgov-repo")
            setup_pane_borders("dgov-repo")

        assert mock_run.call_count == 1

    def test_style_dgov_session_sets_window_and_status_options(self) -> None:
        with (
            patch("dgov.tmux.setup_pane_borders") as mock_borders,
            patch("dgov.tmux._run") as mock_run,
        ):
            style_dgov_session("dgov-repo")

        mock_borders.assert_called_once_with("dgov-repo")
        # Single compound call (all set-option ops joined with ";")
        mock_run.assert_called_once_with(
            [
                "set-option",
                "-t",
                "dgov-repo",
                "window-style",
                "fg=colour247,bg=colour236",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "window-active-style",
                "fg=default,bg=colour234",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "status-style",
                "fg=colour252,bg=colour236",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "status-left",
                " #[bold,fg=colour39]dgov#[default] │ ",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "status-right",
                " #{pane_title} │ %H:%M ",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "set-titles",
                "on",
                ";",
                "set-option",
                "-t",
                "dgov-repo",
                "set-titles-string",
                "#S: #W",
            ],
            silent=True,
        )

    def test_style_worker_and_governor_panes(self) -> None:
        with (
            patch("dgov.tmux.set_pane_option") as mock_set_option,
            patch("dgov.tmux._run") as mock_run,
        ):
            style_worker_pane("%2", "pi")

        assert mock_set_option.call_args_list == [
            call("%2", "pane-border-style", "fg=colour34"),
            call("%2", "pane-active-border-style", "fg=colour34,bold"),
        ]
        mock_run.assert_called_once_with(
            [
                "set-option",
                "-p",
                "-t",
                "%2",
                "pane-border-format",
                " #[fg=colour34,bold]#P "
                "#[default]#{?pane_title,#{pane_title},#{pane_current_command}} ",
            ],
            silent=True,
        )

        with (
            patch("dgov.tmux.set_pane_option") as mock_set_option,
            patch("dgov.tmux._run"),
        ):
            style_worker_pane("%2", "unknown")

        assert mock_set_option.call_args_list == [
            call("%2", "pane-border-style", "fg=colour252"),
            call("%2", "pane-active-border-style", "fg=colour252,bold"),
        ]

        with patch("dgov.tmux._run") as mock_run:
            style_governor_pane("%1")

        mock_run.assert_called_once_with(
            [
                "select-pane",
                "-t",
                "%1",
                "-P",
                "fg=default,bg=colour234",
                ";",
                "select-pane",
                "-t",
                "%1",
                "-T",
                "[gov] main",
            ],
            silent=True,
        )

    def test_style_worker_pane_explicit_color(self) -> None:
        with (
            patch("dgov.tmux.set_pane_option") as mock_set_option,
            patch("dgov.tmux._run") as mock_run,
        ):
            style_worker_pane("%3", "custom-agent", color=99)

        assert mock_set_option.call_args_list == [
            call("%3", "pane-border-style", "fg=colour99"),
            call("%3", "pane-active-border-style", "fg=colour99,bold"),
        ]
        mock_run.assert_called_once_with(
            [
                "set-option",
                "-p",
                "-t",
                "%3",
                "pane-border-format",
                " #[fg=colour99,bold]#P "
                "#[default]#{?pane_title,#{pane_title},#{pane_current_command}} ",
            ],
            silent=True,
        )


class TestComposedHelpers:
    def test_create_utility_pane_sequences_calls(self) -> None:
        with (
            patch("dgov.tmux.split_pane", return_value="%44") as mock_split,
            patch("dgov.tmux.send_command") as mock_send,
            patch("dgov.tmux.set_title") as mock_title,
            patch("dgov.tmux.select_layout") as mock_layout,
        ):
            pane_id = create_utility_pane("lazygit", "[util] lazygit", cwd="/repo")

        assert pane_id == "%44"
        mock_split.assert_called_once_with(cwd="/repo")
        mock_send.assert_called_once_with("%44", "lazygit")
        mock_title.assert_called_once_with("%44", "[util] lazygit")
        mock_layout.assert_called_once_with("tiled")

    def test_send_prompt_via_buffer_uses_temp_buffer_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dgov.tmux.time.time", lambda: 123.456)
        with patch("dgov.tmux._run") as mock_run:
            send_prompt_via_buffer("%5", "Fix the bug")

        assert mock_run.call_args_list == [
            call(["set-buffer", "-b", "dgov-123456", "--", "Fix the bug"]),
            call(["paste-buffer", "-b", "dgov-123456", "-t", "%5"]),
            call(["send-keys", "-t", "%5", "Enter"]),
            call(["delete-buffer", "-b", "dgov-123456"], silent=True),
        ]
