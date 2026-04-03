"""Deterministic kernel primitives for pane and DAG lifecycle.

All kernel classes are pure state machines: (state, event) → (new_state, actions).
No I/O, no blocking, no imports of executor/lifecycle/waiter at module level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from dgov.types import PaneState, WorkerObservation, WorkerPhase

__all__ = [
    "WorkerPhase",
    "WorkerObservation",
    "DagTaskState",
    "DagState",
    "DagKernel",
]

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


# -- DAG Actions (kernel → runtime) --


@dataclass(frozen=True)
class DispatchTask:
    task_slug: str


@dataclass(frozen=True)
class WaitForAny:
    """Wait for any of these tasks' panes to complete."""

    task_slugs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewTask:
    task_slug: str
    pane_slug: str
    review_agent: str | None = None


@dataclass(frozen=True)
class MergeTask:
    task_slug: str
    pane_slug: str
    file_claims: tuple[str, ...] = ()


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
    status: DagState
    merged: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]
    blocked: tuple[str, ...]


@dataclass(frozen=True)
class InterruptGovernor:
    task_slug: str
    pane_slug: str
    reason: str


DagAction = (
    DispatchTask
    | WaitForAny
    | ReviewTask
    | MergeTask
    | SkipTask
    | CloseTask
    | InterruptGovernor
    | DagDone
)


# -- DAG Events (runtime → kernel) --


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
    pane_state: str | PaneState  # "done", "failed", "timed_out", etc.


@dataclass(frozen=True)
class TaskReviewDone:
    task_slug: str
    passed: bool
    verdict: str  # kept str: accepts ReviewVerdict (StrEnum) + raw model strings
    commit_count: int


@dataclass(frozen=True)
class TaskMergeDone:
    task_slug: str
    error: str | None = None


@dataclass(frozen=True)
class MergeConflictDetected:
    """Emitted when a merge conflict is detected and requires manual resolution."""

    task_slug: str
    pane_slug: str
    conflict_details: str | None = None


@dataclass(frozen=True)
class TaskConflictResolved:
    """Emitted when a merge conflict has been manually resolved."""

    task_slug: str
    resolution: ConflictResolution = ConflictResolution.MERGED


@dataclass(frozen=True)
class TaskDispatchDeferred:
    """Emitted when dispatch fails due to capacity exhaustion (DegradationError).

    Task returns to PENDING without burning a retry attempt.
    """

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


DagEvent = (
    TaskDispatched
    | TaskDispatchFailed
    | TaskWaitDone
    | TaskReviewDone
    | TaskMergeDone
    | TaskDispatchDeferred
    | TaskClosed
    | TaskGovernorResumed
    | MergeConflictDetected
    | TaskConflictResolved
)


@dataclass
class DagKernel:
    """Pure state machine for multi-pane DAG orchestration.

    The kernel tracks per-task state and the dependency graph. It emits
    actions for the runtime to execute and consumes events reporting
    outcomes. The execution graph (parallel dispatch by readiness) is
    separate from the merge graph (serial integration in topo order).

    Usage::

        kernel = DagKernel(deps={"b": ("a",), "c": ("a",), "a": ()}, max_concurrent=2)
        actions = kernel.start()
        while not kernel.done:
            for action in actions:
                event = runtime.execute(action)
                actions = kernel.handle(event)
    """

    deps: dict[str, tuple[str, ...]]
    auto_merge: bool = True
    max_concurrent: int = 0  # 0 = unlimited
    skip: frozenset[str] = frozenset()
    review_agents: dict[str, str] = field(default_factory=dict)
    max_retries: int = 0
    task_files: dict[str, tuple[str, ...]] = field(default_factory=dict)  # file claims per task

    # -- internal state --
    state: DagState = DagState.IDLE
    task_states: dict[str, DagTaskState] = field(default_factory=dict)  # noqa: RUF009
    pane_slugs: dict[str, str] = field(default_factory=dict)  # noqa: RUF009
    attempts: dict[str, int] = field(default_factory=dict)  # noqa: RUF009
    merge_order: tuple[str, ...] = ()
    _merge_cursor: int = 0

    def __post_init__(self) -> None:
        if not self.task_states:
            self.task_states = {slug: DagTaskState.PENDING for slug in self.deps}
        for slug in self.deps:
            self.attempts.setdefault(slug, 1)
        if not self.merge_order:
            self.merge_order = tuple(_topo_sort(self.deps))
        # Apply pre-skip: mark skipped tasks and their transitive dependents
        if self.skip:
            skipped = set(self.skip)
            changed = True
            while changed:
                changed = False
                for slug, deps in self.deps.items():
                    if slug in skipped:
                        continue
                    if any(d in skipped for d in deps):
                        skipped.add(slug)
                        changed = True
            for slug in skipped:
                if slug in self.task_states:
                    self.task_states[slug] = DagTaskState.SKIPPED

    def to_dict(self) -> dict:
        """Serialize kernel state for DB persistence."""
        return {
            "deps": self.deps,
            "auto_merge": self.auto_merge,
            "max_concurrent": self.max_concurrent,
            "skip": list(self.skip),
            "review_agents": self.review_agents,
            "max_retries": self.max_retries,
            "task_files": {k: list(v) for k, v in self.task_files.items()},
            "state": self.state.value,
            "task_states": {k: v.value for k, v in self.task_states.items()},
            "pane_slugs": self.pane_slugs,
            "attempts": self.attempts,
            "merge_order": self.merge_order,
            "merge_cursor": self._merge_cursor,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DagKernel:
        """Reconstruct kernel state from DB."""
        # Use field() default_factory for task_states/pane_slugs/attempts
        k = cls(
            deps={k: tuple(v) for k, v in data["deps"].items()},
            auto_merge=data.get("auto_merge", True),
            max_concurrent=data.get("max_concurrent", 0),
            skip=frozenset(data.get("skip", [])),
            review_agents=data.get("review_agents", {}),
            max_retries=data.get("max_retries", 0),
            task_files={k: tuple(v) for k, v in data.get("task_files", {}).items()},
        )
        k.state = DagState(data.get("state", "idle"))
        k.task_states = {k_: DagTaskState(v) for k_, v in data.get("task_states", {}).items()}
        k.pane_slugs = dict(data.get("pane_slugs", {}))
        k.attempts = dict(data.get("attempts", {}))
        k.merge_order = tuple(data.get("merge_order", ()))
        k._merge_cursor = int(data.get("merge_cursor", 0))
        return k

    @property
    def done(self) -> bool:
        return self.state in (DagState.COMPLETED, DagState.FAILED, DagState.PARTIAL)

    # -- public interface --

    def start(self) -> list[DagAction]:
        if self.state != DagState.IDLE:
            raise ValueError(f"DagKernel already started in state {self.state}")
        self.state = DagState.RUNNING
        actions = self._schedule()
        actions.extend(self._check_done())
        return actions

    def handle(self, event: DagEvent) -> list[DagAction]:
        actions: list[DagAction] = []

        # Guard: ignore events for unknown tasks
        task_slug = getattr(event, "task_slug", None)
        if task_slug and task_slug not in self.task_states:
            return []

        if isinstance(event, TaskDispatched):
            # Reject double dispatch — task must be in DISPATCHED state
            if self.task_states.get(event.task_slug) != DagTaskState.DISPATCHED:
                return []
            self.task_states[event.task_slug] = DagTaskState.WAITING
            self.pane_slugs[event.task_slug] = event.pane_slug
            # After dispatch confirmed, check if we should wait
            actions.extend(self._maybe_wait())
            return actions

        if isinstance(event, TaskDispatchDeferred):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.DISPATCHED:
                return []
            # Return to pending without burning a retry
            self.task_states[task] = DagTaskState.PENDING
            # Don't call _schedule() - avoids tight re-dispatch loop
            # Wait for active work to complete and capacity to free up
            return self._maybe_wait()

        if isinstance(event, TaskDispatchFailed):
            task = event.task_slug
            self.task_states[task] = DagTaskState.BLOCKED_ON_GOVERNOR
            pane = self.pane_slugs.get(task, "")
            actions.append(InterruptGovernor(task, pane, reason=f"dispatch_failed: {event.error}"))
            actions.extend(self._schedule())
            actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskWaitDone):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.WAITING:
                return []
            if event.pane_state in (
                PaneState.DONE,
                PaneState.REVIEWED_PASS,
                PaneState.MERGED,
            ):
                self.task_states[task] = DagTaskState.REVIEWING
                review_agent = self.review_agents.get(task)
                actions.append(ReviewTask(task, event.pane_slug, review_agent=review_agent))
            else:
                # Wait failed (timeout or crash) -> block on governor
                self.task_states[task] = DagTaskState.BLOCKED_ON_GOVERNOR
                actions.append(
                    InterruptGovernor(task, event.pane_slug, reason=f"wait_{event.pane_state}")
                )
                # Rest of DAG keeps running (scheduling happens automatically)
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskReviewDone):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.REVIEWING:
                return []
            if event.passed and event.commit_count > 0:
                self.task_states[task] = DagTaskState.MERGE_READY
                if self.auto_merge:
                    actions.extend(self._try_merge())
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            else:
                # Review failed (negative verdict or 0 commits) -> block on governor
                self.task_states[task] = DagTaskState.BLOCKED_ON_GOVERNOR
                pane = self.pane_slugs.get(task, "")
                actions.append(InterruptGovernor(task, pane, reason="review_failed"))
                # Rest of DAG keeps running
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskGovernorResumed):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.BLOCKED_ON_GOVERNOR:
                return []
            if event.action == GovernorAction.RETRY:
                self.attempts[task] = 1
                self.task_states[task] = DagTaskState.PENDING
                actions.extend(self._schedule())
            elif event.action == GovernorAction.FAIL:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            elif event.action == GovernorAction.SKIP:
                self.task_states[task] = DagTaskState.SKIPPED
                actions.extend(self._skip_dependents(task))
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskMergeDone):
            task = event.task_slug
            if self.task_states.get(task) not in (
                DagTaskState.MERGE_READY,
                DagTaskState.MERGING,
                DagTaskState.CONFLICTED,
            ):
                return []
            if event.error and "conflict" in event.error.lower():
                # Merge conflict detected — transition to CONFLICTED for manual resolution
                self.task_states[task] = DagTaskState.CONFLICTED
                reason = f"merge_conflict: {event.error}"
                pane_slug = self.pane_slugs.get(task, "")
                actions.append(InterruptGovernor(task, pane_slug, reason=reason))
                actions.extend(self._schedule())
            elif event.error:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
                pane = self.pane_slugs.get(task, "")
                if pane:
                    actions.append(CloseTask(task, pane, reason="merge_failed"))
            else:
                self.task_states[task] = DagTaskState.MERGED
                self._merge_cursor += 1
                pane = self.pane_slugs.get(task, "")
                if pane:
                    actions.append(CloseTask(task, pane, reason="merged"))
                # Merged task may unblock dependents
                actions.extend(self._schedule())
                # Try merging next in topo order
                actions.extend(self._try_merge())
            actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskClosed):
            task = event.task_slug
            state = self.task_states.get(task)
            if state in (DagTaskState.WAITING, DagTaskState.REVIEWING):
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
            actions.extend(self._check_done())
            return actions

        if isinstance(event, MergeConflictDetected):
            task = event.task_slug
            if self.task_states.get(task) not in (
                DagTaskState.MERGE_READY,
                DagTaskState.MERGING,
            ):
                return []
            self.task_states[task] = DagTaskState.CONFLICTED
            actions.append(
                InterruptGovernor(
                    task,
                    event.pane_slug,
                    reason=f"merge_conflict: {event.conflict_details or 'conflict detected'}",
                )
            )
            actions.extend(self._schedule())
            actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskConflictResolved):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.CONFLICTED:
                return []
            # Resume DAG execution from terminal state
            if self.state in (DagState.FAILED, DagState.PARTIAL):
                self.state = DagState.RUNNING
            if event.resolution == ConflictResolution.MERGED:
                self.task_states[task] = DagTaskState.MERGED
                self._merge_cursor += 1
                actions.extend(self._schedule())
                actions.extend(self._try_merge())
            elif event.resolution == ConflictResolution.ABORTED:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
            elif event.resolution == ConflictResolution.RETRY:
                self.attempts[task] = 1
                self.task_states[task] = DagTaskState.PENDING
                actions.extend(self._schedule())
            actions.extend(self._check_done())
            return actions

        raise ValueError(f"Unknown DagEvent: {event!r}")

    # -- internal scheduling --

    def _deps_met(self, slug: str) -> bool:
        for dep in self.deps.get(slug, ()):
            if self.task_states.get(dep) != DagTaskState.MERGED:
                return False
        return True

    def _active_count(self) -> int:
        active = {DagTaskState.DISPATCHED, DagTaskState.WAITING, DagTaskState.REVIEWING}
        return sum(1 for s in self.task_states.values() if s in active)

    def _has_file_conflict(self, slug: str) -> bool:
        """Check if this task's files overlap with any active (running) task's files.

        File conflict detection prevents parallel execution of tasks that touch
        the same files. Only RUNNING tasks are considered as conflicts —
        PENDING tasks will be checked when they become ready.
        """
        active_states = {DagTaskState.DISPATCHED, DagTaskState.WAITING, DagTaskState.REVIEWING}
        task_files = set(self.task_files.get(slug, ()))
        if not task_files:
            return False  # No files claimed, no conflict possible

        for other_slug, state in self.task_states.items():
            if state not in active_states:
                continue
            if other_slug == slug:
                continue
            other_files = set(self.task_files.get(other_slug, ()))
            if task_files & other_files:  # Set intersection
                return True
        return False

    def _schedule(self) -> list[DagAction]:
        """Emit DispatchTask for ready tasks within concurrency limit."""
        actions: list[DagAction] = []
        active = self._active_count()
        for slug in self.merge_order:
            # Skip if not pending or already in progress
            state = self.task_states.get(slug)
            if state != DagTaskState.PENDING:
                continue
            if not self._deps_met(slug):
                continue
            if self.max_concurrent > 0 and active >= self.max_concurrent:
                break
            # Check file conflicts before dispatching
            if self._has_file_conflict(slug):
                continue  # Skip this task, will retry on next scheduling cycle
            # Atomic transition: PENDING -> DISPATCHED
            self.task_states[slug] = DagTaskState.DISPATCHED
            actions.append(DispatchTask(slug))
            active += 1
        return actions

    def _maybe_wait(self) -> list[DagAction]:
        """Emit WaitForAny if there are waiting tasks."""
        waiting = [
            slug for slug, state in self.task_states.items() if state == DagTaskState.WAITING
        ]
        if waiting:
            return [WaitForAny(tuple(waiting))]
        return []

    def _try_merge(self) -> list[DagAction]:
        """Merge next task in topo order if it's merge-ready."""
        actions: list[DagAction] = []
        while self._merge_cursor < len(self.merge_order):
            slug = self.merge_order[self._merge_cursor]
            state = self.task_states[slug]
            if state == DagTaskState.MERGE_READY:
                self.task_states[slug] = DagTaskState.MERGING
                pane = self.pane_slugs.get(slug, "")
                file_claims = self.task_files.get(slug, ())
                actions.append(MergeTask(slug, pane, file_claims))
                break
            if state in _DAG_TERMINAL:
                # Skip past already-terminal tasks in merge order
                self._merge_cursor += 1
                continue
            # Not yet ready to merge — wait
            break
        return actions

    def _skip_dependents(self, failed_slug: str) -> list[DagAction]:
        """Skip all tasks that transitively depend on the failed task."""
        actions: list[DagAction] = []
        failed = {
            s
            for s, st in self.task_states.items()
            if st in (DagTaskState.FAILED, DagTaskState.SKIPPED)
        }
        failed.add(failed_slug)
        changed = True
        while changed:
            changed = False
            for slug, deps in self.deps.items():
                if self.task_states[slug] in _DAG_TERMINAL:
                    continue
                if self.task_states[slug] != DagTaskState.PENDING:
                    continue
                if any(d in failed for d in deps):
                    self.task_states[slug] = DagTaskState.SKIPPED
                    failed.add(slug)
                    actions.append(SkipTask(slug, reason=f"dependency {failed_slug} failed"))
                    changed = True
        return actions

    def _is_runnable(self, slug: str) -> bool:
        """Return True if this task can ever run in the current DAG state.

        A task is runnable if all its dependencies are either MERGED or themselves runnable.
        If any dependency is FAILED, SKIPPED, CONFLICTED, or BLOCKED_ON_GOVERNOR,
        this task is effectively dead until a human intervenes.
        """
        state = self.task_states[slug]
        if state in _DAG_TERMINAL:
            return False
        if state in (DagTaskState.CONFLICTED, DagTaskState.BLOCKED_ON_GOVERNOR):
            return False
        # If it's already active, it's definitely runnable
        if state not in (DagTaskState.PENDING, DagTaskState.BLOCKED_ON_GOVERNOR):
            return True

        for dep in self.deps.get(slug, ()):
            dep_state = self.task_states[dep]
            if dep_state == DagTaskState.MERGED:
                continue
            if dep_state in (
                DagTaskState.FAILED,
                DagTaskState.SKIPPED,
                DagTaskState.CONFLICTED,
                DagTaskState.BLOCKED_ON_GOVERNOR,
            ):
                return False
            # Dependency is pending/waiting/etc — check its own runnability
            if not self._is_runnable(dep):
                return False
        return True

    def _check_done(self) -> list[DagAction]:
        """Check if the DAG is complete and emit DagDone if so."""
        if self.state != DagState.RUNNING:
            return []

        # Find any tasks that are still moving (not terminal and still runnable)
        moving = [s for s in self.task_states if self._is_runnable(s)]
        if moving:
            return self._maybe_wait()

        # No more tasks can move — either completed or blocked
        merged = tuple(s for s, st in self.task_states.items() if st == DagTaskState.MERGED)
        failed = tuple(s for s, st in self.task_states.items() if st == DagTaskState.FAILED)
        skipped = tuple(s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED)
        blocked = tuple(
            s
            for s, st in self.task_states.items()
            if st in (DagTaskState.BLOCKED_ON_GOVERNOR, DagTaskState.CONFLICTED)
        )

        if failed or skipped or blocked:
            self.state = DagState.PARTIAL if merged else DagState.FAILED
        else:
            self.state = DagState.COMPLETED

        return [
            DagDone(
                status=self.state, merged=merged, failed=failed, skipped=skipped, blocked=blocked
            )
        ]


# -- Utilities --


# ---------------------------------------------------------------------------
# Semantic manifest — declared and observed file sets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticManifest:
    """What a worker claimed it would touch vs what it actually touched.

    Populated in two phases:
    - Dispatch: file_claims from ContextPacket (declared intent)
    - Completion: paths_written from git diff (observed reality)

    Used at merge time to detect stale dependencies: if main changed
    files that overlap with paths_written since base_sha, the merge
    may be unsafe.
    """

    base_sha: str = ""
    file_claims: tuple[str, ...] = ()
    paths_written: tuple[str, ...] = ()

    @property
    def claim_violations(self) -> tuple[str, ...]:
        if not self.file_claims:
            return ()
        claim_set = set(self.file_claims)
        return tuple(p for p in self.paths_written if p not in claim_set)


def _topo_sort(deps: dict[str, tuple[str, ...]]) -> list[str]:
    """Stable topological sort for merge ordering."""
    visited: set[str] = set()
    order: list[str] = []

    def _visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(deps.get(node, ())):
            _visit(dep)
        order.append(node)

    for slug in sorted(deps):
        _visit(slug)
    return order
