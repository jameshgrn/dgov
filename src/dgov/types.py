"""Core type definitions for dgov — minimal dependencies version."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class TaskState(StrEnum):
    """Task lifecycle states for the governor event loop."""

    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    REVIEWED_PASS = "reviewed_pass"
    REVIEWED_FAIL = "reviewed_fail"
    MERGED = "merged"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"
    ABANDONED = "abandoned"


# -- Runner events --


@dataclass(frozen=True)
class WorkerExit:
    """Worker exit event — source of truth for completion."""

    task_slug: str
    pane_slug: str
    exit_code: int
    output_dir: str


# -- Worktree type --


class Worktree(NamedTuple):
    """Represents a git worktree sandbox."""

    path: Path
    branch: str
    commit: str
