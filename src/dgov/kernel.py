"""Deterministic kernel primitives for DAG lifecycle.

Pure state machine: (state, event) -> (new_state, actions).
No I/O, no blocking, no imports beyond actions and types.

HFT-inspired: explicit dispatch table, scan-based merge, no string mangling.
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
from dgov.types import PaneState

__all__ = [
    "DagTaskState",
    "DagState",
    "DagKernel",
]

logger = logging.getLogger(__name__)


class DagTaskState(StrEnum):
    PENDING = "pending"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    MERGE_READY = "merge_ready"
    MERGING = "merging"
    # Terminal
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"


_TERMINAL = frozenset({DagTaskState.MERGED, DagTaskState.FAILED, DagTaskState.SKIPPED})


class DagState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class DagKernel:
    """Pure state machine for multi-task DAG orchestration.

    Invariants:
      - Merge is serial, topo-ordered, scan-based (no cursor drift).
      - Terminal tasks are skipped during merge scan (failures don't block).
      - Dispatch table is explicit (no string mangling).
    """

    deps: dict[str, tuple[str, ...]]
    task_files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_retries: int = 3

    # Mutable state
    task_states: dict[str, DagTaskState] = field(init=False)
    pane_slugs: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    merge_order: list[str] = field(init=False)

    def __post_init__(self):
        self.task_states = {slug: DagTaskState.PENDING for slug in self.deps}
        self.merge_order = _topological_sort(self.deps)

    # -- Public API --

    @property
    def done(self) -> bool:
        return all(st in _TERMINAL for st in self.task_states.values())

    @property
    def status(self) -> DagState:
        if all(st == DagTaskState.PENDING for st in self.task_states.values()):
            return DagState.IDLE
        if not self.done:
            return DagState.RUNNING
        has_failed = any(st == DagTaskState.FAILED for st in self.task_states.values())
        has_merged = any(st == DagTaskState.MERGED for st in self.task_states.values())
        if has_failed:
            return DagState.PARTIAL if has_merged else DagState.FAILED
        return DagState.COMPLETED

    def start(self) -> list[DagAction]:
        """Kick off: dispatch all tasks whose deps are satisfied."""
        return self._schedule()

    def handle(self, event: DagEvent) -> list[DagAction]:
        """Feed an event, get back actions. O(1) dispatch."""
        method_name = _DISPATCH.get(type(event))
        if method_name is None:
            return []
        actions: list[DagAction] = getattr(self, method_name)(event)
        if self.done:
            actions.append(self._summary())
        return actions

    def to_dict(self) -> dict:
        return {
            "task_states": {k: v.value for k, v in self.task_states.items()},
            "pane_slugs": self.pane_slugs,
            "attempts": self.attempts,
            "merge_order": self.merge_order,
        }

    # -- Event handlers --

    def _on_dispatched(self, event: TaskDispatched) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != DagTaskState.PENDING:
            return []
        self.task_states[event.task_slug] = DagTaskState.WAITING
        self.pane_slugs[event.task_slug] = event.pane_slug
        waiting = [s for s, st in self.task_states.items() if st == DagTaskState.WAITING]
        return [WaitForAny(tuple(waiting))]

    def _on_wait_done(self, event: TaskWaitDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != DagTaskState.WAITING:
            return []
        if event.pane_state == PaneState.DONE:
            self.task_states[event.task_slug] = DagTaskState.REVIEWING
            return [ReviewTask(event.task_slug, event.pane_slug)]
        # Fail-closed: any non-DONE state triggers governor interrupt
        pane = self.pane_slugs.get(event.task_slug, "")
        return [InterruptGovernor(event.task_slug, pane, reason=str(event.pane_state))]

    def _on_review_done(self, event: TaskReviewDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != DagTaskState.REVIEWING:
            return []
        if event.passed:
            self.task_states[event.task_slug] = DagTaskState.MERGE_READY
            return self._try_merge()
        self.task_states[event.task_slug] = DagTaskState.FAILED
        return [*self._try_merge(), *self._schedule()]

    def _on_merge_done(self, event: TaskMergeDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != DagTaskState.MERGING:
            return []
        if event.error:
            self.task_states[event.task_slug] = DagTaskState.FAILED
        else:
            self.task_states[event.task_slug] = DagTaskState.MERGED
        return [*self._try_merge(), *self._schedule()]

    def _on_governor_resumed(self, event: TaskGovernorResumed) -> list[DagAction]:
        slug = event.task_slug
        current = self.task_states.get(slug)
        # Guard: only accept from WAITING (interrupt) or FAILED (manual retry).
        # Prevents accidentally un-merging or un-reviewing a task.
        if current not in (DagTaskState.WAITING, DagTaskState.FAILED):
            return []
        if event.action == GovernorAction.RETRY:
            self.task_states[slug] = DagTaskState.PENDING
            self.attempts[slug] = self.attempts.get(slug, 0) + 1
            return self._schedule()
        if event.action == GovernorAction.SKIP:
            self.task_states[slug] = DagTaskState.SKIPPED
            return [*self._try_merge(), *self._schedule()]
        if event.action == GovernorAction.FAIL:
            self.task_states[slug] = DagTaskState.FAILED
            return [*self._try_merge(), *self._schedule()]
        return []

    # -- Internal primitives --

    def _schedule(self) -> list[DagAction]:
        """Dispatch all PENDING tasks whose deps are fully MERGED."""
        actions: list[DagAction] = []
        for slug, st in self.task_states.items():
            if st == DagTaskState.PENDING:
                deps = self.deps.get(slug, ())
                if all(self.task_states[d] == DagTaskState.MERGED for d in deps):
                    actions.append(DispatchTask(slug))
        return actions

    def _try_merge(self) -> list[DagAction]:
        """Scan topo order for next mergeable task. One merge at a time.

        Scan-based: skips terminal states, blocks on in-progress tasks.
        No cursor to drift out of sync.
        """
        if any(st == DagTaskState.MERGING for st in self.task_states.values()):
            return []
        for slug in self.merge_order:
            st = self.task_states[slug]
            if st == DagTaskState.MERGE_READY:
                self.task_states[slug] = DagTaskState.MERGING
                return [
                    MergeTask(
                        slug,
                        self.pane_slugs.get(slug, ""),
                        file_claims=self.task_files.get(slug, ()),
                    )
                ]
            if st in _TERMINAL:
                continue  # skip merged/failed/skipped — don't block
            return []  # in-progress task — preserve topo-order merge guarantee
        return []

    def _summary(self) -> DagDone:
        return DagDone(
            status=self.status,
            merged=tuple(s for s, st in self.task_states.items() if st == DagTaskState.MERGED),
            failed=tuple(s for s, st in self.task_states.items() if st == DagTaskState.FAILED),
            skipped=tuple(s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED),
            blocked=(),
        )


# -- Dispatch table: event type -> handler method name. O(1), no regex. --

_DISPATCH: dict[type, str] = {
    TaskDispatched: "_on_dispatched",
    TaskWaitDone: "_on_wait_done",
    TaskReviewDone: "_on_review_done",
    TaskMergeDone: "_on_merge_done",
    TaskGovernorResumed: "_on_governor_resumed",
}


def _topological_sort(deps: dict[str, tuple[str, ...]]) -> list[str]:
    """DFS post-order topo sort. Deterministic (sorted input). Raises on cycles."""
    order: list[str] = []
    visited: set[str] = set()
    path: set[str] = set()

    def _visit(node: str) -> None:
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
