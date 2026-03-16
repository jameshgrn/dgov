"""Tests for dgov.backend — WorkerBackend protocol and TmuxBackend."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import dgov.backend as backend_mod
from dgov.backend import TmuxBackend, WorkerBackend, get_backend, set_backend

pytestmark = pytest.mark.unit


class TestProtocolConformance:
    def test_tmux_backend_is_instance_of_protocol(self):
        assert isinstance(TmuxBackend(), WorkerBackend)

    def test_all_protocol_methods_exist_on_tmux_backend(self):
        expected = {
            "create_pane",
            "create_worker_pane",
            "destroy",
            "is_alive",
            "send_input",
            "capture_output",
            "current_command",
            "bulk_info",
            "set_title",
            "style",
            "start_logging",
            "stop_logging",
            "send_prompt_via_buffer",
            "send_keys",
            "setup_pane_borders",
            "set_pane_option",
            "select_layout",
        }
        actual = {name for name in dir(TmuxBackend) if not name.startswith("_")}
        assert expected <= actual, f"Missing: {expected - actual}"


class TestFactory:
    def setup_method(self):
        backend_mod._backend = None

    def teardown_method(self):
        backend_mod._backend = None

    def test_get_backend_returns_tmux_by_default(self):
        b = get_backend()
        assert isinstance(b, TmuxBackend)

    def test_get_backend_is_singleton(self):
        b1 = get_backend()
        b2 = get_backend()
        assert b1 is b2

    def test_set_backend_overrides_default(self):
        class FakeBackend:
            def create_pane(
                self,
                *,
                cwd: str,
                target: str | None = None,
                env: dict[str, str] | None = None,
            ) -> str:
                return "fake-id"

            def destroy(self, worker_id: str) -> None:
                pass

            def is_alive(self, worker_id: str) -> bool:
                return False

            def send_input(self, worker_id: str, text: str) -> None:
                pass

            def capture_output(self, worker_id: str, lines: int = 30) -> str | None:
                return None

            def current_command(self, worker_id: str) -> str:
                return ""

            def bulk_info(self) -> dict[str, dict[str, str]]:
                return {}

            def set_title(self, worker_id: str, title: str) -> None:
                pass

            def style(self, worker_id: str, agent: str, *, color: int | None = None) -> None:
                pass

            def start_logging(self, worker_id: str, log_file: str) -> None:
                pass

            def stop_logging(self, worker_id: str) -> None:
                pass

            def send_prompt_via_buffer(self, worker_id: str, prompt: str) -> None:
                pass

            def send_keys(self, worker_id: str, keys: list[str]) -> None:
                pass

            def setup_pane_borders(self) -> None:
                pass

            def set_pane_option(self, worker_id: str, option: str, value: str) -> None:
                pass

            def configure_worker_pane(
                self, worker_id: str, title: str, agent: str, *, color: int | None = None
            ) -> None:
                pass

            def select_layout(self, layout: str = "tiled") -> None:
                pass

            def create_worker_pane(
                self,
                *,
                cwd: str,
                env: dict[str, str] | None = None,
            ) -> str:
                return "fake-worker"

        fake = FakeBackend()
        set_backend(fake)
        assert get_backend() is fake
        assert isinstance(fake, WorkerBackend)

    def test_mock_backend_create_pane(self):
        class MockBackend:
            def create_pane(
                self,
                *,
                cwd: str,
                target: str | None = None,
                env: dict[str, str] | None = None,
            ) -> str:
                return f"mock-{cwd}"

            def destroy(self, worker_id: str) -> None:
                pass

            def is_alive(self, worker_id: str) -> bool:
                return True

            def send_input(self, worker_id: str, text: str) -> None:
                pass

            def capture_output(self, worker_id: str, lines: int = 30) -> str | None:
                return "output"

            def current_command(self, worker_id: str) -> str:
                return "python"

            def bulk_info(self) -> dict[str, dict[str, str]]:
                return {"mock-1": {"title": "test", "current_command": "python"}}

            def set_title(self, worker_id: str, title: str) -> None:
                pass

            def style(self, worker_id: str, agent: str, *, color: int | None = None) -> None:
                pass

            def start_logging(self, worker_id: str, log_file: str) -> None:
                pass

            def stop_logging(self, worker_id: str) -> None:
                pass

            def send_prompt_via_buffer(self, worker_id: str, prompt: str) -> None:
                pass

            def send_keys(self, worker_id: str, keys: list[str]) -> None:
                pass

            def setup_pane_borders(self) -> None:
                pass

            def set_pane_option(self, worker_id: str, option: str, value: str) -> None:
                pass

            def configure_worker_pane(
                self, worker_id: str, title: str, agent: str, *, color: int | None = None
            ) -> None:
                pass

            def select_layout(self, layout: str = "tiled") -> None:
                pass

            def create_worker_pane(
                self,
                *,
                cwd: str,
                env: dict[str, str] | None = None,
            ) -> str:
                return f"mock-worker-{cwd}"

        mock = MockBackend()
        set_backend(mock)
        b = get_backend()
        assert b.create_pane(cwd="/tmp") == "mock-/tmp"
        assert b.is_alive("any") is True
        assert b.capture_output("any") == "output"
        assert b.current_command("any") == "python"
        assert b.bulk_info() == {"mock-1": {"title": "test", "current_command": "python"}}


class TestTmuxBackendDelegation:
    def test_create_pane_delegates_to_split_pane(self):
        env = {"FOO": "bar"}
        with patch("dgov.tmux.split_pane", return_value="%5") as mock_split_pane:
            worker_id = TmuxBackend().create_pane(cwd="/tmp", target="%1", env=env)

        assert worker_id == "%5"
        mock_split_pane.assert_called_once_with(cwd="/tmp", target="%1", env=env)

    def test_destroy_delegates_to_kill_pane(self):
        with patch("dgov.tmux.kill_pane") as mock_kill_pane:
            TmuxBackend().destroy("%%5")

        mock_kill_pane.assert_called_once_with("%%5")

    def test_is_alive_delegates_to_pane_exists(self):
        with patch("dgov.tmux.pane_exists", return_value=True) as mock_pane_exists:
            is_alive = TmuxBackend().is_alive("%%5")

        assert is_alive is True
        mock_pane_exists.assert_called_once_with("%%5")

    def test_send_input_delegates_to_send_command(self):
        with patch("dgov.tmux.send_command") as mock_send_command:
            TmuxBackend().send_input("%%5", "echo hi")

        mock_send_command.assert_called_once_with("%%5", "echo hi")

    def test_capture_output_delegates_to_capture_pane(self):
        with patch("dgov.tmux.capture_pane", return_value="output") as mock_capture_pane:
            output = TmuxBackend().capture_output("%%5", lines=50)

        assert output == "output"
        mock_capture_pane.assert_called_once_with("%%5", lines=50)

    def test_capture_output_returns_none_on_runtime_error(self):
        with patch(
            "dgov.tmux.capture_pane", side_effect=RuntimeError("pane gone")
        ) as mock_capture:
            output = TmuxBackend().capture_output("%%5", lines=50)

        assert output is None
        mock_capture.assert_called_once_with("%%5", lines=50)

    def test_current_command_delegates_to_current_command(self):
        with patch("dgov.tmux.current_command", return_value="python") as mock_current_command:
            command = TmuxBackend().current_command("%%5")

        assert command == "python"
        mock_current_command.assert_called_once_with("%%5")

    def test_bulk_info_delegates_to_bulk_pane_info(self):
        info = {"%%5": {"title": "worker", "current_command": "python"}}
        with patch("dgov.tmux.bulk_pane_info", return_value=info) as mock_bulk_pane_info:
            result = TmuxBackend().bulk_info()

        assert result == info
        mock_bulk_pane_info.assert_called_once_with()

    def test_set_title_delegates_to_set_title(self):
        with patch("dgov.tmux.set_title") as mock_set_title:
            TmuxBackend().set_title("%%5", "worker")

        mock_set_title.assert_called_once_with("%%5", "worker")

    def test_style_delegates_to_style_worker_pane(self):
        with patch("dgov.tmux.style_worker_pane") as mock_style_worker_pane:
            TmuxBackend().style("%%5", "codex", color=214)

        mock_style_worker_pane.assert_called_once_with("%%5", "codex", color=214)

    def test_start_logging_delegates_to_start_logging(self):
        with patch("dgov.tmux.start_logging") as mock_start_logging:
            TmuxBackend().start_logging("%%5", "/tmp/worker.log")

        mock_start_logging.assert_called_once_with("%%5", "/tmp/worker.log")

    def test_stop_logging_delegates_to_stop_logging(self):
        with patch("dgov.tmux.stop_logging") as mock_stop_logging:
            TmuxBackend().stop_logging("%%5")

        mock_stop_logging.assert_called_once_with("%%5")

    def test_send_prompt_via_buffer_delegates_to_send_prompt_via_buffer(self):
        with patch("dgov.tmux.send_prompt_via_buffer") as mock_send_prompt_via_buffer:
            TmuxBackend().send_prompt_via_buffer("%%5", "prompt text")

        mock_send_prompt_via_buffer.assert_called_once_with("%%5", "prompt text")

    def test_send_keys_delegates_to_run(self):
        keys = ["C-c", "Enter"]
        with patch("dgov.tmux._run") as mock_run:
            TmuxBackend().send_keys("%%5", keys)

        mock_run.assert_called_once_with(["send-keys", "-t", "%%5", "C-c", "Enter"])

    def test_setup_pane_borders_delegates_to_setup_pane_borders(self):
        with patch("dgov.tmux.setup_pane_borders") as mock_setup_pane_borders:
            TmuxBackend().setup_pane_borders()

        mock_setup_pane_borders.assert_called_once_with()

    def test_set_pane_option_delegates_to_set_pane_option(self):
        with patch("dgov.tmux.set_pane_option") as mock_set_pane_option:
            TmuxBackend().set_pane_option("%%5", "pane-border-style", "fg=green")

        mock_set_pane_option.assert_called_once_with("%%5", "pane-border-style", "fg=green")

    def test_select_layout_delegates_to_select_layout(self):
        with patch("dgov.tmux.select_layout") as mock_select_layout:
            TmuxBackend().select_layout("even-horizontal")

        mock_select_layout.assert_called_once_with("even-horizontal")
