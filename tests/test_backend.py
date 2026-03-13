"""Tests for dgov.backend — WorkerBackend protocol and TmuxBackend."""

from __future__ import annotations

import dgov.backend as backend_mod
from dgov.backend import TmuxBackend, WorkerBackend, get_backend, set_backend


class TestProtocolConformance:
    def test_tmux_backend_is_instance_of_protocol(self):
        assert isinstance(TmuxBackend(), WorkerBackend)

    def test_all_protocol_methods_exist_on_tmux_backend(self):
        expected = {
            "create_pane",
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
            def create_pane(self, *, cwd: str, target: str | None = None) -> str:
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

        fake = FakeBackend()
        set_backend(fake)
        assert get_backend() is fake
        assert isinstance(fake, WorkerBackend)

    def test_mock_backend_create_pane(self):
        class MockBackend:
            def create_pane(self, *, cwd: str, target: str | None = None) -> str:
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

        mock = MockBackend()
        set_backend(mock)
        b = get_backend()
        assert b.create_pane(cwd="/tmp") == "mock-/tmp"
        assert b.is_alive("any") is True
        assert b.capture_output("any") == "output"
        assert b.current_command("any") == "python"
        assert b.bulk_info() == {"mock-1": {"title": "test", "current_command": "python"}}
