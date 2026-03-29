"""Deterministic kernel primitives for pane and DAG lifecycle.

All kernel classes are pure state machines: (state, event) → (new_state, actions).
No I/O, no blocking, no imports of executor/lifecycle/waiter at module level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from dgov.persistence import PaneState

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


_DAG_TERMINAL = frozenset({DagTaskState.MERGED, DagTaskState.FAILED, DagTaskState.SKIPPED})


class DagState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


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
class RetryTask:
    task_slug: str
    pane_slug: str
    attempt: int


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
    | RetryTask
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
class TaskClosed:
    task_slug: str


@dataclass(frozen=True)
class TaskRetryStarted:
    task_slug: str
    new_pane_slug: str
    attempt: int


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
    | TaskClosed
    | TaskRetryStarted
    | TaskGovernorResumed
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
    max_retries: int = 3

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
            max_retries=data.get("max_retries", 3),
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

        if isinstance(event, TaskDispatchFailed):
            self.task_states[event.task_slug] = DagTaskState.FAILED
            actions.extend(self._skip_dependents(event.task_slug))
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
                # Wait failed (timeout or crash) -> try retry/escalate
                curr_attempt = self.attempts.get(task, 1)
                if curr_attempt <= self.max_retries:
                    self.task_states[task] = DagTaskState.DISPATCHED
                    actions.append(RetryTask(task, event.pane_slug, curr_attempt))
                else:
                    # BLOCK on governor instead of hard failure
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
            # On retry attempts, trust passed=True even with commit_count=0:
            # retry panes may lack context_packet, causing commit detection to fail
            # even though the worker committed and review passed.
            is_retry = self.attempts.get(task, 1) > 1
            if event.passed and (event.commit_count > 0 or is_retry):
                self.task_states[task] = DagTaskState.MERGE_READY
                if self.auto_merge:
                    actions.extend(self._try_merge())
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            else:
                # Review failed (negative verdict or 0 commits) -> retry/escalate
                curr_attempt = self.attempts.get(task, 1)
                if curr_attempt <= self.max_retries:
                    self.task_states[task] = DagTaskState.DISPATCHED
                    pane = self.pane_slugs.get(task, "")
                    actions.append(RetryTask(task, pane, curr_attempt))
                else:
                    # BLOCK on governor instead of hard failure
                    self.task_states[task] = DagTaskState.BLOCKED_ON_GOVERNOR
                    pane = self.pane_slugs.get(task, "")
                    actions.append(InterruptGovernor(task, pane, reason="review_failed"))
                    # Rest of DAG keeps running
                    actions.extend(self._schedule())
                    actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskRetryStarted):
            task = event.task_slug
            if self.task_states.get(task) != DagTaskState.DISPATCHED:
                return []
            self.task_states[task] = DagTaskState.WAITING
            self.pane_slugs[task] = event.new_pane_slug
            self.attempts[task] = event.attempt + 1
            # MUST return wait action or loop will exhaust without events
            actions.extend(self._maybe_wait())
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
            if self.task_states.get(task) not in (DagTaskState.MERGE_READY, DagTaskState.MERGING):
                return []
            if event.error:
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

    def _schedule(self) -> list[DagAction]:
        """Emit DispatchTask for ready tasks within concurrency limit."""
        actions: list[DagAction] = []
        active = self._active_count()
        for slug in self.merge_order:
            if self.task_states[slug] != DagTaskState.PENDING:
                continue
            if not self._deps_met(slug):
                continue
            if self.max_concurrent > 0 and active >= self.max_concurrent:
                break
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
                actions.append(MergeTask(slug, pane))
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

    def _check_done(self) -> list[DagAction]:
        """Check if the DAG is complete and emit DagDone if so."""
        if self.state != DagState.RUNNING:
            return []

        # MERGE_READY is terminal when auto_merge=False
        # BLOCKED_ON_GOVERNOR is terminal for the automatic run loop (requires human)
        effectively_terminal = _DAG_TERMINAL | {DagTaskState.BLOCKED_ON_GOVERNOR}
        if not self.auto_merge:
            effectively_terminal |= {DagTaskState.MERGE_READY}

        non_terminal = [s for s, st in self.task_states.items() if st not in effectively_terminal]
        if non_terminal:
            return self._maybe_wait()

        merged = tuple(s for s, st in self.task_states.items() if st == DagTaskState.MERGED)
        failed = tuple(s for s, st in self.task_states.items() if st == DagTaskState.FAILED)
        skipped = tuple(s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED)
        blocked = tuple(
            s for s, st in self.task_states.items() if st == DagTaskState.BLOCKED_ON_GOVERNOR
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
# Unified worker observation — shared vocabulary for completion + classification
# ---------------------------------------------------------------------------


class WorkerPhase(StrEnum):
    """What the worker is doing right now.

    Produced by both completion detection (_is_done) and output
    classification (classify_output). This is the shared vocabulary.
    """

    WORKING = "working"
    COMMITTING = "committing"
    DONE = "done"
    FAILED = "failed"
    STUCK = "stuck"
    IDLE = "idle"
    WAITING_INPUT = "waiting_input"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerObservation:
    """Unified observation of a worker's state.

    Combines structural signals (done file, exit code, commits, liveness)
    with behavioral classification (output analysis). This is the single
    source of truth for "what state is this worker in?"
    """

    slug: str
    phase: WorkerPhase
    alive: bool = True
    has_commits: bool = False
    has_done_signal: bool = False
    has_exit_signal: bool = False
    exit_code: int | None = None
    classification: str = "unknown"
    reason: str | None = None


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
