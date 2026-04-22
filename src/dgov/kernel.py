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
    CleanupTask,
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
)
from dgov.types import TaskState

__all__ = [
    "DagKernel",
    "DagState",
    "TaskState",
]

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({
    TaskState.MERGED,
    TaskState.FAILED,
    TaskState.SKIPPED,
    TaskState.ABANDONED,
    TaskState.TIMED_OUT,
})


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
    task_states: dict[str, TaskState] = field(init=False)
    pane_slugs: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    merge_order: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.task_states = {slug: TaskState.PENDING for slug in self.deps}
        self.merge_order = _topological_sort(self.deps)

    # -- Public API --

    @property
    def done(self) -> bool:
        return all(st in _TERMINAL for st in self.task_states.values())

    @property
    def status(self) -> DagState:
        if all(st == TaskState.PENDING for st in self.task_states.values()):
            return DagState.IDLE
        if not self.done:
            return DagState.RUNNING
        _BAD = (TaskState.FAILED, TaskState.ABANDONED, TaskState.TIMED_OUT)
        has_failed = any(st in _BAD for st in self.task_states.values())
        has_merged = any(st == TaskState.MERGED for st in self.task_states.values())
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
        if self.task_states.get(event.task_slug) != TaskState.PENDING:
            return []
        self.task_states[event.task_slug] = TaskState.ACTIVE
        self.pane_slugs[event.task_slug] = event.pane_slug
        return []

    def _on_wait_done(self, event: TaskWaitDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != TaskState.ACTIVE:
            return []

        if event.task_state == TaskState.DONE:
            self.task_states[event.task_slug] = TaskState.REVIEWING
            return [ReviewTask(event.task_slug, event.pane_slug)]

        # Terminal states from rehydration or cleanup
        if event.task_state in (TaskState.ABANDONED, TaskState.TIMED_OUT):
            self.task_states[event.task_slug] = event.task_state
            self._cascade_failure(event.task_slug)
            return [CleanupTask(event.task_slug), *self._try_merge(), *self._schedule()]

        # Fail-closed: any other non-DONE state triggers governor interrupt (retry logic)
        # This includes TaskState.FAILED which the runner handles via retry count.
        return [InterruptGovernor(event.task_slug, event.pane_slug, reason=str(event.task_state))]

    def _on_review_done(self, event: TaskReviewDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != TaskState.REVIEWING:
            return []
        if event.passed:
            self.task_states[event.task_slug] = TaskState.REVIEWED_PASS
            return self._try_merge()
        # Read-scope violations are retriable: the worker edited a
        # files.read file. Route through InterruptGovernor (retry logic)
        # instead of cascading terminal failure to dependents.
        if event.verdict == "read_scope_violation":
            self.task_states[event.task_slug] = TaskState.FAILED
            return [InterruptGovernor(event.task_slug, "", reason="read_scope_violation")]
        self.task_states[event.task_slug] = TaskState.FAILED
        self._cascade_failure(event.task_slug)
        return [CleanupTask(event.task_slug), *self._try_merge(), *self._schedule()]

    def _on_merge_done(self, event: TaskMergeDone) -> list[DagAction]:
        if self.task_states.get(event.task_slug) != TaskState.MERGING:
            return []
        if event.error:
            self.task_states[event.task_slug] = TaskState.FAILED
            self._cascade_failure(event.task_slug)
            # Runner's _merge method handles worktree cleanup for this transition
            # to allow for inspection on settlement rejection.
            actions: list[DagAction] = []
        else:
            self.task_states[event.task_slug] = TaskState.MERGED
            actions = []
        return [*actions, *self._try_merge(), *self._schedule()]

    def _on_governor_resumed(self, event: TaskGovernorResumed) -> list[DagAction]:
        slug = event.task_slug
        current = self.task_states.get(slug)
        # Guard: accept from PENDING (dispatch failure), WAITING (interrupt), or terminal
        # states (manual). Prevents accidentally un-merging or un-reviewing a task.
        if current not in (
            TaskState.PENDING,
            TaskState.ACTIVE,
            TaskState.FAILED,
            TaskState.ABANDONED,
            TaskState.TIMED_OUT,
            TaskState.SKIPPED,
        ):
            return []
        if event.action == GovernorAction.RETRY:
            self.task_states[slug] = TaskState.PENDING
            self.attempts[slug] = self.attempts.get(slug, 0) + 1
            return self._schedule()
        if event.action == GovernorAction.SKIP:
            self.task_states[slug] = TaskState.SKIPPED
            self._cascade_failure(slug)
            return [CleanupTask(slug), *self._try_merge(), *self._schedule()]
        if event.action == GovernorAction.FAIL:
            self.task_states[slug] = TaskState.FAILED
            self._cascade_failure(slug)
            return [CleanupTask(slug), *self._try_merge(), *self._schedule()]
        return []

    # -- Internal primitives --

    def _cascade_failure(self, failed_slug: str) -> None:
        """Mark all PENDING tasks transitively depending on failed_slug as SKIPPED."""
        blocked: set[str] = set()
        changed = True
        while changed:
            changed = False
            for slug, st in self.task_states.items():
                if st != TaskState.PENDING or slug in blocked:
                    continue
                deps = self.deps.get(slug, ())
                if failed_slug in deps or any(d in blocked for d in deps):
                    blocked.add(slug)
                    changed = True
        for slug in blocked:
            self.task_states[slug] = TaskState.SKIPPED
            logger.warning("SKIPPED %s (blocked by failed %s)", slug, failed_slug)

    def _schedule(self) -> list[DagAction]:
        """Dispatch all PENDING tasks whose deps are fully MERGED."""
        actions: list[DagAction] = []
        for slug, st in self.task_states.items():
            if st == TaskState.PENDING:
                deps = self.deps.get(slug, ())
                if all(self.task_states[d] == TaskState.MERGED for d in deps):
                    actions.append(DispatchTask(slug))
        return actions

    def _try_merge(self) -> list[DagAction]:
        """Scan topo order for next mergeable task. One merge at a time.

        Scan-based: skips terminal states, blocks on in-progress tasks.
        No cursor to drift out of sync.
        """
        if any(st == TaskState.MERGING for st in self.task_states.values()):
            return []
        for slug in self.merge_order:
            st = self.task_states[slug]
            if st == TaskState.REVIEWED_PASS:
                self.task_states[slug] = TaskState.MERGING
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
            merged=tuple(s for s, st in self.task_states.items() if st == TaskState.MERGED),
            failed=tuple(s for s, st in self.task_states.items() if st == TaskState.FAILED),
            skipped=tuple(s for s, st in self.task_states.items() if st == TaskState.SKIPPED),
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
