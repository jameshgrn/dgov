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
    """Pure state machine for multi-pane DAG orchestration."""

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
                merged = any(s == DagTaskState.MERGED for s in self.task_states.values())
                return DagState.PARTIAL if merged else DagState.FAILED
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
        """React to an external event via focused sub-handlers (Pillar #1)."""
        actions: list[DagAction] = []

        # Dispatch to specific handler based on event type
        handler_name = f"_handle_{_to_snake(event.__class__.__name__)}"
        handler = getattr(self, handler_name, self._handle_unknown)

        actions.extend(handler(event))

        if self.done:
            actions.append(self._emit_done())

        return actions

    # --- Event Handlers (Pillar #4: Determinism) ---

    def _handle_task_dispatched(self, event: TaskDispatched) -> list[DagAction]:
        task = event.task_slug
        if self.task_states.get(task) == DagTaskState.PENDING:
            self.task_states[task] = DagTaskState.WAITING
            self.pane_slugs[task] = event.pane_slug
            return self._maybe_wait()
        return []

    def _handle_task_wait_done(self, event: TaskWaitDone) -> list[DagAction]:
        task = event.task_slug
        if self.task_states.get(task) == DagTaskState.WAITING:
            if event.pane_state == PaneState.DONE:
                self.task_states[task] = DagTaskState.REVIEWING
                return [ReviewTask(task, event.pane_slug)]

            # Pillar #10: Fail-closed on error
            pane_slug = self.pane_slugs.get(task, "")
            return [InterruptGovernor(task, pane_slug, reason=str(event.pane_state))]
        return []

    def _handle_task_review_done(self, event: TaskReviewDone) -> list[DagAction]:
        task = event.task_slug
        if self.task_states.get(task) == DagTaskState.REVIEWING:
            if event.passed:
                self.task_states[task] = DagTaskState.MERGE_READY
                return self._maybe_merge()

            self.task_states[task] = DagTaskState.FAILED
            return self._schedule()
        return []

    def _handle_task_merge_done(self, event: TaskMergeDone) -> list[DagAction]:
        task = event.task_slug
        actions = []
        if self.task_states.get(task) == DagTaskState.MERGING:
            if event.error:
                self.task_states[task] = DagTaskState.FAILED
            else:
                self.task_states[task] = DagTaskState.MERGED
                self._merge_cursor += 1

            actions.extend(self._maybe_merge())
            actions.extend(self._schedule())
        return actions

    def _handle_task_governor_resumed(self, event: TaskGovernorResumed) -> list[DagAction]:
        task = event.task_slug
        if event.action == GovernorAction.RETRY:
            self.task_states[task] = DagTaskState.PENDING
            self.attempts[task] = self.attempts.get(task, 0) + 1
            return self._schedule()
        if event.action == GovernorAction.FAIL:
            self.task_states[task] = DagTaskState.FAILED
            return self._schedule()
        return []

    def _handle_unknown(self, event: DagEvent) -> list[DagAction]:
        logger.debug("Kernel: ignoring unhandled event %s", type(event).__name__)
        return []

    # --- Internal Primitives ---

    def _schedule(self) -> list[DagAction]:
        actions: list[DagAction] = []
        for slug, state in self.task_states.items():
            if state == DagTaskState.PENDING:
                if all(
                    self.task_states[d] == DagTaskState.MERGED for d in self.deps.get(slug, ())
                ):
                    actions.append(DispatchTask(slug))
        return actions

    def _maybe_wait(self) -> list[DagAction]:
        waiting = [s for s, st in self.task_states.items() if st == DagTaskState.WAITING]
        return [WaitForAny(tuple(waiting))] if waiting else []

    def _maybe_merge(self) -> list[DagAction]:
        if self._merge_cursor >= len(self.merge_order):
            return []

        target = self.merge_order[self._merge_cursor]
        if self.task_states[target] == DagTaskState.MERGE_READY:
            self.task_states[target] = DagTaskState.MERGING
            return [
                MergeTask(
                    target,
                    self.pane_slugs.get(target, ""),
                    file_claims=self.task_files.get(target, ()),
                )
            ]
        return []

    def _emit_done(self) -> DagDone:
        return DagDone(
            status=self.status,
            merged=tuple(s for s, st in self.task_states.items() if st == DagTaskState.MERGED),
            failed=tuple(s for s, st in self.task_states.items() if st == DagTaskState.FAILED),
            skipped=tuple(s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED),
            blocked=tuple(
                s for s, st in self.task_states.items() if st == DagTaskState.BLOCKED_ON_GOVERNOR
            ),
        )


def _topological_sort(deps: dict[str, tuple[str, ...]]) -> list[str]:
    order, visited, path = [], set(), set()

    def _visit(node):
        if node in path:
            raise ValueError(f"Cycle: {node}")
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


def _to_snake(name: str) -> str:
    import re

    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
