"""Core type definitions for dgov — minimal dependencies version."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class ConstitutionalViolation(ValueError):
    """Raised when a plan unit violates department ownership boundaries.

    This occurs when a unit's write-capable file claims touch paths owned by
    a department, but the unit lacks the required explicit summary opt-in.
    """

    pass


class TaskState(StrEnum):
    """Task lifecycle states for the governor event loop."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    REVIEWING = "reviewing"
    REVIEWED_PASS = "reviewed_pass"
    REVIEWED_FAIL = "reviewed_fail"
    MERGING = "merging"
    MERGED = "merged"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"
    ABANDONED = "abandoned"
    SKIPPED = "skipped"


# -- Runner events --


@dataclass(frozen=True)
class WorkerExit:
    """Worker exit event — source of truth for completion."""

    task_slug: str
    pane_slug: str
    exit_code: int
    output_dir: str
    last_error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


# -- Worktree type --


class Worktree(NamedTuple):
    """Represents a git worktree sandbox."""

    path: Path
    branch: str
    commit: str
