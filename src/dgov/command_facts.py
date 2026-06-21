"""First-class command execution evidence records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CommandExecutionFact:
    """Objective record of a command executed by a validation or verify gate."""

    gate: str
    source: str
    command: str
    outcome: Literal["completed", "timed_out"]
    duration_s: float
    exit_code: int | None = None
    timeout_s: float | None = None
    log_path: str | None = None
    warning_count: int | None = None

    def __post_init__(self) -> None:
        if self.outcome == "completed":
            if self.exit_code is None:
                raise ValueError("CommandExecutionFact: completed outcome requires exit_code")
            if self.timeout_s is not None:
                raise ValueError("CommandExecutionFact: completed outcome cannot set timeout_s")
            return
        if self.outcome == "timed_out":
            if self.exit_code is not None:
                raise ValueError("CommandExecutionFact: timed_out outcome cannot set exit_code")
            if self.timeout_s is None:
                raise ValueError("CommandExecutionFact: timed_out outcome requires timeout_s")
            return
        raise ValueError(f"CommandExecutionFact: unknown outcome {self.outcome!r}")
