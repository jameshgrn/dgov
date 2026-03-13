"""Abstract worker backend interface and tmux implementation.

Decouples pane lifecycle from tmux so alternative backends
(Docker, SSH, etc.) can be swapped in.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class WorkerBackend(Protocol):
    """Abstract interface for worker execution backends."""

    def create_pane(self, *, cwd: str, target: str | None = None) -> str:
        """Create a new worker pane/container. Return a worker_id."""
        ...

    def destroy(self, worker_id: str) -> None:
        """Kill/remove the worker."""
        ...

    def is_alive(self, worker_id: str) -> bool:
        """Check if the worker is still running."""
        ...

    def send_input(self, worker_id: str, text: str) -> None:
        """Send text input to the worker (like typing in a terminal)."""
        ...

    def capture_output(self, worker_id: str, lines: int = 30) -> str | None:
        """Capture recent output. Returns None if worker is dead."""
        ...

    def current_command(self, worker_id: str) -> str:
        """Get the foreground process name."""
        ...

    def bulk_info(self) -> dict[str, dict[str, str]]:
        """Get info for all workers. Returns {worker_id: {title, current_command}}."""
        ...

    def set_title(self, worker_id: str, title: str) -> None:
        """Set a display title for the worker."""
        ...

    def style(self, worker_id: str, agent: str, *, color: int | None = None) -> None:
        """Apply visual styling (noop for non-visual backends)."""
        ...

    def start_logging(self, worker_id: str, log_file: str) -> None:
        """Start capturing worker output to a file."""
        ...

    def stop_logging(self, worker_id: str) -> None:
        """Stop capturing worker output."""
        ...

    def send_prompt_via_buffer(self, worker_id: str, prompt: str) -> None:
        """Send a large prompt via paste buffer (for send-keys transport)."""
        ...

    def setup_pane_borders(self) -> None:
        """Set pane border styling (tmux global/session options)."""
        ...

    def set_pane_option(self, worker_id: str, option: str, value: str) -> None:
        """Set a pane-level tmux option."""
        ...

    def select_layout(self, layout: str = "tiled") -> None:
        """Apply a tmux layout to the current window."""
        ...


class TmuxBackend:
    """tmux-based worker backend — wraps dgov.tmux functions."""

    def create_pane(self, *, cwd: str, target: str | None = None) -> str:
        from dgov import tmux

        return tmux.split_pane(cwd=cwd, target=target)

    def destroy(self, worker_id: str) -> None:
        from dgov import tmux

        tmux.kill_pane(worker_id)

    def is_alive(self, worker_id: str) -> bool:
        from dgov import tmux

        return tmux.pane_exists(worker_id)

    def send_input(self, worker_id: str, text: str) -> None:
        from dgov import tmux

        tmux.send_command(worker_id, text)

    def capture_output(self, worker_id: str, lines: int = 30) -> str | None:
        from dgov import tmux

        try:
            return tmux.capture_pane(worker_id, lines=lines)
        except RuntimeError:
            return None

    def current_command(self, worker_id: str) -> str:
        from dgov import tmux

        return tmux.current_command(worker_id)

    def bulk_info(self) -> dict[str, dict[str, str]]:
        from dgov import tmux

        return tmux.bulk_pane_info()

    def set_title(self, worker_id: str, title: str) -> None:
        from dgov import tmux

        tmux.set_title(worker_id, title)

    def style(self, worker_id: str, agent: str, *, color: int | None = None) -> None:
        from dgov import tmux

        tmux.style_worker_pane(worker_id, agent, color=color)

    def start_logging(self, worker_id: str, log_file: str) -> None:
        from dgov import tmux

        tmux.start_logging(worker_id, log_file)

    def stop_logging(self, worker_id: str) -> None:
        from dgov import tmux

        tmux.stop_logging(worker_id)

    def send_prompt_via_buffer(self, worker_id: str, prompt: str) -> None:
        from dgov import tmux

        tmux.send_prompt_via_buffer(worker_id, prompt)

    def setup_pane_borders(self) -> None:
        from dgov import tmux

        tmux.setup_pane_borders()

    def set_pane_option(self, worker_id: str, option: str, value: str) -> None:
        from dgov import tmux

        tmux.set_pane_option(worker_id, option, value)

    def select_layout(self, layout: str = "tiled") -> None:
        from dgov import tmux

        tmux.select_layout(layout)


_backend: WorkerBackend | None = None


def get_backend() -> WorkerBackend:
    """Return the active backend, defaulting to TmuxBackend."""
    global _backend
    if _backend is None:
        _backend = TmuxBackend()
    return _backend


def set_backend(backend: WorkerBackend) -> None:
    """Override the active backend (useful for testing or alternative runtimes)."""
    global _backend
    _backend = backend
