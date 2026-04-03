"""Core Action and Event primitives for the dgov lifecycle.

Pillar #1: Separation of Powers - These are the 'messages' passed between the Governor
and the implementation layers. They are immutable data structures.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Optional, Tuple, Union

if TYPE_CHECKING:
    pass


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
class SkipTask:
    task_slug: str
    reason: str


@dataclass(frozen=True)
class CloseTask:
    task_slug: str
    pane_slug: str
    reason: str


@dataclass(frozen=True)
class DagDone:
    status: Any  # Should be DagState
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
    SkipTask,
    CloseTask,
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
    pane_state: Union[str, Any]  # str | PaneState


@dataclass(frozen=True)
class TaskReviewDone:
    task_slug: str
    passed: bool
    verdict: str  # accepts ReviewVerdict (StrEnum) + raw model strings
    commit_count: int


@dataclass(frozen=True)
class TaskMergeDone:
    task_slug: str
    error: Optional[str] = None


@dataclass(frozen=True)
class MergeConflictDetected:
    """Emitted when a merge conflict is detected and requires manual resolution."""

    task_slug: str
    pane_slug: str
    conflict_details: Optional[str] = None


@dataclass(frozen=True)
class TaskConflictResolved:
    """Emitted when a merge conflict has been manually resolved."""

    task_slug: str
    resolution: Any  # Should be ConflictResolution


@dataclass(frozen=True)
class TaskDispatchDeferred:
    """Emitted when dispatch fails due to capacity exhaustion."""

    task_slug: str
    reason: str


@dataclass(frozen=True)
class TaskClosed:
    task_slug: str


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
    TaskDispatchDeferred,
    TaskClosed,
    TaskGovernorResumed,
    MergeConflictDetected,
    TaskConflictResolved,
]
