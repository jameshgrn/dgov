"""Deterministic kernel primitives for pane and DAG lifecycle.

All kernel classes are pure state machines: (state, event) → (new_state, actions).
No I/O, no blocking, no imports of executor/lifecycle/waiter at module level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from dgov.actions import (
    DagAction,
    DagDone,
    DagEvent,
    DispatchTask,
    GovernorAction,
    InterruptGovernor,
    MergeTask,
    ReviewTask,
    TaskDispatched,
    TaskGovernorResumed,
    TaskMergeDone,
    TaskReviewDone,
    TaskWaitDone,
    WaitForAny,
)
from dgov.types import PaneState, WorkerObservation, WorkerPhase

__all__ = [
    "WorkerPhase",
    "WorkerObservation",
    "DagTaskState",
    "DagState",
    "DagKernel",
    "ConflictResolution",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DAG Kernel — multi-pane orchestration with dependency tracking
# ---------------------------------------------------------------------------


class DagTaskState(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    MERGE_READY = "merge_ready"
    MERGING = "merging"
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED_ON_GOVERNOR = "blocked_on_governor"
    CONFLICTED = "conflicted"


_DAG_TERMINAL = frozenset({DagTaskState.MERGED, DagTaskState.FAILED, DagTaskState.SKIPPED})


class DagState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class ConflictResolution(StrEnum):
    MERGED = "merged"
    ABORTED = "aborted"
    RETRY = "retry"


@dataclass
class DagKernel:
    """Pure state machine for multi-pane DAG orchestration.

    The kernel tracks per-task state and the dependency graph. It emits
    actions for the runtime to execute and consumes events reporting
    the outcome of those actions.
    """

    deps: dict[str, tuple[str, ...]]
    task_files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    auto_merge: bool = True
    max_retries: int = 3

    # State tracking
    task_states: dict[str, DagTaskState] = field(init=False)
    pane_slugs: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    merge_order: list[str] = field(init=False)
    _merge_cursor: int = 0

    def __post_init__(self):
        self.task_states = {slug: DagTaskState.PENDING for slug in self.deps}
        self.merge_order = _topological_sort(self.deps)

    @property
    def done(self) -> bool:
        """True if all tasks are in terminal states."""
        return all(state in _DAG_TERMINAL for state in self.task_states.values())

    @property
    def status(self) -> DagState:
        """Derived overall DAG state."""
        if not any(state != DagTaskState.PENDING for state in self.task_states.values()):
            return DagState.IDLE
        if self.done:
            if any(state == DagTaskState.FAILED for state in self.task_states.values()):
                return (
                    DagState.FAILED
                    if not any(s == DagTaskState.MERGED for s in self.task_states.values())
                    else DagState.PARTIAL
                )
            return DagState.COMPLETED
        return DagState.RUNNING

    def to_dict(self) -> dict:
        return {
            "task_states": {k: v.value for k, v in self.task_states.items()},
            "pane_slugs": self.pane_slugs,
            "attempts": self.attempts,
            "merge_order": self.merge_order,
            "merge_cursor": self._merge_cursor,
        }

    def start(self) -> list[DagAction]:
        """Initialize DAG execution and return initial dispatch actions."""
        return self._schedule()

    def handle(self, event: DagEvent) -> list[DagAction]:
        """React to an external event and return resulting actions."""
        actions: list[DagAction] = []
        task = event.task_slug

        if isinstance(event, TaskDispatched):
            if self.task_states.get(task) == DagTaskState.PENDING:
                self.task_states[task] = DagTaskState.WAITING
                self.pane_slugs[task] = event.pane_slug
                actions.extend(self._maybe_wait())

        elif isinstance(event, TaskWaitDone):
            if self.task_states.get(task) == DagTaskState.WAITING:
                # Basic transition: WAITING -> REVIEWING (happy path auto-pass)
                if event.pane_state == PaneState.DONE:
                    self.task_states[task] = DagTaskState.REVIEWING
                    actions.append(ReviewTask(task, event.pane_slug))
                else:
                    # Pillar #10: Fail-closed on error
                    reason = str(event.pane_state)
                    pane_slug = self.pane_slugs.get(task, "")
                    actions.append(InterruptGovernor(task, pane_slug, reason=reason))

        elif isinstance(event, TaskReviewDone):
            if self.task_states.get(task) == DagTaskState.REVIEWING:
                if event.passed:
                    self.task_states[task] = DagTaskState.MERGE_READY
                    actions.extend(self._maybe_merge())
                else:
                    self.task_states[task] = DagTaskState.FAILED
                    actions.extend(self._schedule())

        elif isinstance(event, TaskMergeDone):
            if self.task_states.get(task) == DagTaskState.MERGING:
                if event.error:
                    self.task_states[task] = DagTaskState.FAILED
                else:
                    self.task_states[task] = DagTaskState.MERGED
                    self._merge_cursor += 1

                actions.extend(self._maybe_merge())
                actions.extend(self._schedule())

        elif isinstance(event, TaskGovernorResumed):
            # External intervention (human or auto-retry)
            if event.action == GovernorAction.RETRY:
                self.task_states[task] = DagTaskState.PENDING
                self.attempts[task] = self.attempts.get(task, 0) + 1
                actions.extend(self._schedule())
            elif event.action == GovernorAction.FAIL:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._schedule())

        if self.done:
            actions.append(
                DagDone(
                    status=self.status,
                    merged=tuple(
                        s for s, st in self.task_states.items() if st == DagTaskState.MERGED
                    ),
                    failed=tuple(
                        s for s, st in self.task_states.items() if st == DagTaskState.FAILED
                    ),
                    skipped=tuple(
                        s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED
                    ),
                    blocked=tuple(
                        s
                        for s, st in self.task_states.items()
                        if st == DagTaskState.BLOCKED_ON_GOVERNOR
                    ),
                )
            )

        return actions

    def _schedule(self) -> list[DagAction]:
        """Find pending tasks with met dependencies and emit DispatchTask actions."""
        actions: list[DagAction] = []
        for slug, state in self.task_states.items():
            if state == DagTaskState.PENDING:
                # Check deps
                deps_met = True
                for dep in self.deps.get(slug, ()):
                    if self.task_states[dep] != DagTaskState.MERGED:
                        deps_met = False
                        break

                if deps_met:
                    actions.append(DispatchTask(slug))
        return actions

    def _maybe_wait(self) -> list[DagAction]:
        """Return WaitForAny if multiple tasks are active."""
        waiting = [s for s, st in self.task_states.items() if st == DagTaskState.WAITING]
        if waiting:
            return [WaitForAny(tuple(waiting))]
        return []

    def _maybe_merge(self) -> list[DagAction]:
        """Try to advance the merge cursor."""
        if self._merge_cursor >= len(self.merge_order):
            return []

        target = self.merge_order[self._merge_cursor]
        if self.task_states[target] == DagTaskState.MERGE_READY:
            self.task_states[target] = DagTaskState.MERGING
            pane_slug = self.pane_slugs.get(target, "")
            return [MergeTask(target, pane_slug, file_claims=self.task_files.get(target, ()))]

        return []


def _topological_sort(deps: dict[str, tuple[str, ...]]) -> list[str]:
    """Simple DFS topological sort."""
    order = []
    visited = set()
    path = set()

    def _visit(node):
        if node in path:
            raise ValueError(f"Circular dependency involving {node}")
        if node in visited:
            return
        path.add(node)
        visited.add(node)
        for dep in deps.get(node, ()):
            _visit(dep)
        path.remove(node)
        order.append(node)

    for node in sorted(deps.keys()):
        _visit(node)

    return order
