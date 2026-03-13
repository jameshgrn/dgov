"""Unit tests for dgov.tmux — thin tmux command wrappers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dgov.tmux import (
    _run,
    capture_pane,
    current_command,
    kill_pane,
    list_panes,
    pane_exists,
    select_layout,
    select_pane,
    send_command,
    send_prompt_via_buffer,
    set_title,
    split_pane,
)

pytestmark = pytest.mark.unit


def _mock_subprocess(monkeypatch, stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Monkeypatch subprocess.run for all tmux tests."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    mock.stderr = stderr
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
    return mock


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


class TestRun:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="  output  \n")
        result = _run(["list-sessions"])
        assert result == "output"

    def test_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, returncode=1, stderr="no server")
        with pytest.raises(RuntimeError, match="tmux"):
            _run(["list-sessions"])

    def test_failure_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, returncode=1, stderr="no server")
        # Should not raise when silent=True
        result = _run(["list-sessions"], silent=True)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# split_pane
# ---------------------------------------------------------------------------


class TestSplitPane:
    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.stdout = "%5\n"
            mock.returncode = 0
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        result = split_pane()
        assert result == "%5"
        assert "split-window" in captured["cmd"]

    def test_with_cwd_and_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.stdout = "%6\n"
            mock.returncode = 0
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        split_pane(cwd="/tmp/repo", target="%1")
        assert "-c" in captured["cmd"]
        assert "/tmp/repo" in captured["cmd"]
        assert "-t" in captured["cmd"]


# ---------------------------------------------------------------------------
# send_command
# ---------------------------------------------------------------------------


class TestSendCommand:
    def test_sends_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        send_command("%5", "ls -la")
        assert "send-keys" in captured["cmd"]
        assert "%5" in captured["cmd"]
        assert "ls -la" in captured["cmd"]
        assert "Enter" in captured["cmd"]


# ---------------------------------------------------------------------------
# set_title
# ---------------------------------------------------------------------------


class TestSetTitle:
    def test_sets_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        set_title("%5", "my-task")
        assert "select-pane" in captured["cmd"]
        assert "-T" in captured["cmd"]
        assert "my-task" in captured["cmd"]


# ---------------------------------------------------------------------------
# capture_pane
# ---------------------------------------------------------------------------


class TestCapturePane:
    def test_captures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="line1\nline2\nline3\n")
        result = capture_pane("%5", lines=3)
        assert "line1" in result


# ---------------------------------------------------------------------------
# pane_exists
# ---------------------------------------------------------------------------


class TestPaneExists:
    def test_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="%5")
        assert pane_exists("%5") is True

    def test_not_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="", returncode=1)
        assert pane_exists("%5") is False


# ---------------------------------------------------------------------------
# current_command
# ---------------------------------------------------------------------------


class TestCurrentCommand:
    def test_returns_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="zsh")
        result = current_command("%5")
        assert result == "zsh"


# ---------------------------------------------------------------------------
# kill_pane
# ---------------------------------------------------------------------------


class TestKillPane:
    def test_kills_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        kill_pane("%5")
        assert "kill-pane" in captured["cmd"]


# ---------------------------------------------------------------------------
# list_panes
# ---------------------------------------------------------------------------


class TestListPanes:
    def test_parses_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "%1|task-1|120|40\n%2|task-2|120|40\n"
        _mock_subprocess(monkeypatch, stdout=output)
        panes = list_panes()
        assert len(panes) == 2
        assert panes[0]["pane_id"] == "%1"
        assert panes[0]["title"] == "task-1"
        assert panes[0]["width"] == "120"
        assert panes[0]["height"] == "40"

    def test_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch, stdout="")
        panes = list_panes()
        assert panes == []

    def test_malformed_lines_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "%1|task-1|120|40\nbadline\n%2|t|80|30\n"
        _mock_subprocess(monkeypatch, stdout=output)
        panes = list_panes()
        assert len(panes) == 2


# ---------------------------------------------------------------------------
# select_pane / select_layout
# ---------------------------------------------------------------------------


class TestSelectPaneLayout:
    def test_select_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch)
        select_pane("%5")  # Should not raise

    def test_select_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_subprocess(monkeypatch)
        select_layout("tiled")  # Should not raise


# ---------------------------------------------------------------------------
# send_prompt_via_buffer
# ---------------------------------------------------------------------------


class TestSendPromptViaBuffer:
    def test_sends_via_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        send_prompt_via_buffer("%5", "Fix the bug")
        # Should have: set-buffer, paste-buffer, send-keys Enter, delete-buffer
        assert len(calls) == 4
        assert "set-buffer" in calls[0]
        assert "paste-buffer" in calls[1]
        assert "send-keys" in calls[2]
        assert "delete-buffer" in calls[3]


class TestTmuxPaneManagement:
    """Unit tests for dgov/tmux.py functions using monkeypatched subprocess.run."""

    def test_split_pane_horizontal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify correct tmux split-window command constructed with -h flag."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])  # Skip 'tmux' prefix
            result = MagicMock()
            result.returncode = 0
            result.stdout.strip.return_value = "#pane123"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import split_pane

        pane_id = split_pane(cwd="/tmp/test", target="window1")

        assert "-h" in call_args[0]
        assert "-P" in call_args[0]
        assert "-F" in call_args[0]
        assert "#{pane_id}" in call_args[0]
        assert [
            "split-window",
            "-h",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            "window1",
            "-c",
            "/tmp/test",
        ] == call_args[0]
        assert pane_id == "#pane123"

    def test_split_pane_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default split-window command with -h flag."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            result.stdout.strip.return_value = "#pane456"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import split_pane

        pane_id = split_pane()

        assert call_args[0] == ["split-window", "-h", "-P", "-F", "#{pane_id}"]
        assert pane_id == "#pane456"

    def test_send_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify send-keys command with proper escaping."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import send_command

        send_command("#pane123", "echo hello world")

        assert call_args[0] == ["send-keys", "-t", "#pane123", "echo hello world", "Enter"]

    def test_capture_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock subprocess to return captured output."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            result.stdout.strip.return_value = "line1\nline2\noutput"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import capture_pane

        output = capture_pane("#pane123", lines=50)

        assert call_args[0] == ["capture-pane", "-t", "#pane123", "-p", "-S", "-50"]
        assert output == "line1\nline2\noutput"

    def test_pane_exists_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock returncode 0, verify True."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            result.stdout.strip.return_value = "#pane123"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import pane_exists

        exists = pane_exists("#pane123")

        assert call_args[0] == ["display-message", "-t", "#pane123", "-p", "#{pane_id}"]
        assert exists is True

    def test_pane_exists_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock returncode 1 (exception raised), verify False."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 1
            result.stderr.strip.return_value = "pane does not exist"
            raise RuntimeError(f"tmux {' '.join(cmd)}: {result.stderr.strip()}")

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import pane_exists

        exists = pane_exists("#nonexistent")

        assert call_args[0] == ["display-message", "-t", "#nonexistent", "-p", "#{pane_id}"]
        assert exists is False

    def test_kill_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify kill-pane command constructed."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import kill_pane

        kill_pane("#pane123")

        assert call_args[0] == ["kill-pane", "-t", "#pane123"]

    def test_list_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock output, verify parsed pane list."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            result.stdout.strip.return_value = "#pane123|My Pane|80|24\n#pane456|Another|120|40\n"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import list_panes

        panes = list_panes()

        assert call_args[0] == [
            "list-panes",
            "-F",
            "#{pane_id}|#{pane_title}|#{pane_width}|#{pane_height}",
        ]
        assert len(panes) == 2
        assert panes[0]["pane_id"] == "#pane123"
        assert panes[0]["title"] == "My Pane"
        assert panes[0]["width"] == "80"
        assert panes[0]["height"] == "24"
        assert panes[1]["pane_id"] == "#pane456"
        assert panes[1]["title"] == "Another"

    def test_select_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify select-pane command."""
        call_args: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs) -> MagicMock:
            call_args.append(cmd[1:])
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        from dgov.tmux import select_pane

        select_pane("#pane123")

        assert call_args[0] == ["select-pane", "-t", "#pane123"]
