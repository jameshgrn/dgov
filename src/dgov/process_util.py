"""Shared subprocess utilities for process-group cleanup."""

from __future__ import annotations

import os
import signal
from typing import Protocol


class _PopenLike(Protocol):
    """Minimal interface for process-like objects."""

    pid: int

    def kill(self) -> None: ...


def kill_process_group(proc: _PopenLike) -> None:
    """Kill a process and its entire process group via SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            return
