"""Core Action and Event primitives for the dgov lifecycle.

Pillar #1: Separation of Powers - These are the 'messages' passed between the Governor
and the implementation layers. They are immutable data structures.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Tuple, Union

# --- Actions (Kernel -> Runner) ---


@dataclass(frozen=True)
class DispatchTask:
    task_slug: str


@dataclass(frozen=True)
class WaitForAny:
    """Wait for any of these tasks' panes to complete."""

    task_slugs: Tuple[str, ...]


@dataclass(frozen=True)
class ReviewTask:
    task_slug: str
    pane_slug: str
    review_agent: Optional[str] = None


@dataclass(frozen=True)
class MergeTask:
    task_slug: str
    pane_slug: str
    file_claims: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DagDone:
    status: str  # DagState value
    merged: Tuple[str, ...]
    failed: Tuple[str, ...]
    skipped: Tuple[str, ...]
    blocked: Tuple[str, ...]


@dataclass(frozen=True)
class InterruptGovernor:
    task_slug: str
    pane_slug: str
    reason: str


DagAction = Union[
    DispatchTask,
    WaitForAny,
    ReviewTask,
    MergeTask,
    InterruptGovernor,
    DagDone,
]


# --- Events (Runner -> Kernel) ---


@dataclass(frozen=True)
class TaskDispatched:
    task_slug: str
    pane_slug: str


@dataclass(frozen=True)
class TaskDispatchFailed:
    task_slug: str
    error: str


@dataclass(frozen=True)
class TaskWaitDone:
    task_slug: str
    pane_slug: str
    pane_state: str  # PaneState value


@dataclass(frozen=True)
class TaskReviewDone:
    task_slug: str
    passed: bool
    verdict: str
    commit_count: int


@dataclass(frozen=True)
class TaskMergeDone:
    task_slug: str
    error: Optional[str] = None


class GovernorAction(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class TaskGovernorResumed:
    task_slug: str
    action: GovernorAction


DagEvent = Union[
    TaskDispatched,
    TaskDispatchFailed,
    TaskWaitDone,
    TaskReviewDone,
    TaskMergeDone,
    TaskGovernorResumed,
]
