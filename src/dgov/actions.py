"""Core Action and Event primitives for the dgov lifecycle.

Pillar #1: Separation of Powers - These are the 'messages' passed between the Governor
and the implementation layers. They are immutable data structures.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# --- Actions (Kernel -> Runner) ---


@dataclass(frozen=True)
class DispatchTask:
    task_slug: str


@dataclass(frozen=True)
class ReviewTask:
    task_slug: str
    pane_slug: str


@dataclass(frozen=True)
class MergeTask:
    task_slug: str
    pane_slug: str
    file_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagDone:
    status: str  # DagState value
    merged: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]
    blocked: tuple[str, ...]


@dataclass(frozen=True)
class CleanupTask:
    task_slug: str


@dataclass(frozen=True)
class InterruptGovernor:
    task_slug: str
    pane_slug: str
    reason: str


DagAction = DispatchTask | ReviewTask | MergeTask | CleanupTask | InterruptGovernor | DagDone


# --- Events (Runner -> Kernel) ---


@dataclass(frozen=True)
class TaskDispatched:
    task_slug: str
    pane_slug: str


@dataclass(frozen=True)
class TaskWaitDone:
    task_slug: str
    pane_slug: str
    task_state: str


@dataclass(frozen=True)
class TaskReviewDone:
    task_slug: str
    passed: bool
    verdict: str
    commit_count: int


@dataclass(frozen=True)
class TaskMergeDone:
    task_slug: str
    error: str | None = None


class GovernorAction(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class TaskGovernorResumed:
    task_slug: str
    action: GovernorAction


DagEvent = TaskDispatched | TaskWaitDone | TaskReviewDone | TaskMergeDone | TaskGovernorResumed
