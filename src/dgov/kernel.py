"""Deterministic kernel primitives for pane and DAG lifecycle.

All kernel classes are pure state machines: (state, event) → (new_state, actions).
No I/O, no blocking, no imports of executor/lifecycle/waiter at module level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dgov.executor import CleanupOnlyResult, MergeOnlyResult, ReviewOnlyResult, WaitOnlyResult


class KernelState(StrEnum):
    START = "start"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    MERGING = "merging"
    REVIEW_PENDING = "review_pending"
    REVIEWED_PASS = "reviewed_pass"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class WaitForPane:
    slug: str


@dataclass(frozen=True)
class ReviewPane:
    slug: str


@dataclass(frozen=True)
class MergePane:
    slug: str


@dataclass(frozen=True)
class CleanupPane:
    slug: str
    state: str
    failure_stage: str | None = None


KernelAction = WaitForPane | ReviewPane | MergePane | CleanupPane


@dataclass(frozen=True)
class WaitCompleted:
    result: WaitOnlyResult


@dataclass(frozen=True)
class ReviewCompleted:
    result: ReviewOnlyResult


@dataclass(frozen=True)
class MergeCompleted:
    result: MergeOnlyResult


@dataclass(frozen=True)
class CleanupCompleted:
    result: CleanupOnlyResult


KernelEvent = WaitCompleted | ReviewCompleted | MergeCompleted | CleanupCompleted


@dataclass
class PostDispatchKernel:
    auto_merge: bool = True
    state: KernelState = KernelState.START

    def start(self, slug: str) -> list[KernelAction]:
        if self.state is not KernelState.START:
            raise ValueError(f"Kernel already started in state {self.state}")
        self.state = KernelState.WAITING
        return [WaitForPane(slug)]

    def start_review(self, slug: str) -> list[KernelAction]:
        if self.state is not KernelState.START:
            raise ValueError(f"Kernel already started in state {self.state}")
        self.state = KernelState.REVIEWING
        return [ReviewPane(slug)]

    def handle(self, event: KernelEvent) -> list[KernelAction]:
        match self.state, event:
            case KernelState.WAITING, WaitCompleted(result=result):
                if result.state != "completed":
                    self.state = KernelState.FAILED
                    return [
                        CleanupPane(
                            result.slug,
                            state="failed",
                            failure_stage=result.failure_stage,
                        )
                    ]
                self.state = KernelState.REVIEWING
                return [ReviewPane(result.slug)]

            case KernelState.REVIEWING, ReviewCompleted(result=result):
                if result.error is not None:
                    self.state = KernelState.FAILED
                    return [CleanupPane(result.slug, state="failed", failure_stage="review")]
                if result.verdict != "safe":
                    self.state = KernelState.REVIEW_PENDING
                    return [CleanupPane(result.slug, state="review_pending")]
                if result.commit_count == 0:
                    # 0 commits is valid for verification/already-done tasks
                    self.state = KernelState.COMPLETED
                    return [CleanupPane(result.slug, state="closed")]
                if not self.auto_merge:
                    self.state = KernelState.REVIEWED_PASS
                    return [CleanupPane(result.slug, state="review_pending")]
                self.state = KernelState.MERGING
                return [MergePane(result.slug)]

            case KernelState.MERGING, MergeCompleted(result=result):
                if result.error is not None:
                    self.state = KernelState.FAILED
                    return [CleanupPane(result.slug, state="failed", failure_stage="merge")]
                self.state = KernelState.COMPLETED
                return [CleanupPane(result.slug, state="completed")]

            case KernelState.REVIEW_PENDING, CleanupCompleted():
                return []

            case KernelState.REVIEWED_PASS, CleanupCompleted():
                return []

            case KernelState.COMPLETED, CleanupCompleted():
                return []

            case KernelState.FAILED, CleanupCompleted():
                return []

            case _:
                raise ValueError(f"Illegal kernel transition: state={self.state} event={event!r}")


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
    status: str
    merged: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]


DagAction = DispatchTask | WaitForAny | ReviewTask | MergeTask | SkipTask | CloseTask | DagDone


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
    pane_state: str  # "done", "failed", "timed_out", etc.


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


@dataclass(frozen=True)
class TaskClosed:
    task_slug: str


DagEvent = (
    TaskDispatched
    | TaskDispatchFailed
    | TaskWaitDone
    | TaskReviewDone
    | TaskMergeDone
    | TaskClosed
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

    # -- internal state --
    state: DagState = DagState.IDLE
    task_states: dict[str, DagTaskState] = field(default_factory=dict)  # noqa: RUF009
    pane_slugs: dict[str, str] = field(default_factory=dict)  # noqa: RUF009
    merge_order: tuple[str, ...] = ()
    _merge_cursor: int = 0

    def __post_init__(self) -> None:
        if not self.task_states:
            self.task_states = {slug: DagTaskState.PENDING for slug in self.deps}
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
            if event.pane_state in ("done", "reviewed_pass", "merged"):
                self.task_states[task] = DagTaskState.REVIEWING
                actions.append(ReviewTask(task, event.pane_slug))
            else:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
                actions.append(CloseTask(task, event.pane_slug, reason="worker_failed"))
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskReviewDone):
            task = event.task_slug
            if event.passed and event.commit_count > 0:
                self.task_states[task] = DagTaskState.MERGE_READY
                if self.auto_merge:
                    actions.extend(self._try_merge())
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            else:
                self.task_states[task] = DagTaskState.FAILED
                actions.extend(self._skip_dependents(task))
                pane = self.pane_slugs.get(task, "")
                if pane:
                    actions.append(CloseTask(task, pane, reason="review_failed"))
                actions.extend(self._schedule())
                actions.extend(self._check_done())
            return actions

        if isinstance(event, TaskMergeDone):
            task = event.task_slug
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
        effectively_terminal = _DAG_TERMINAL | (
            {DagTaskState.MERGE_READY} if not self.auto_merge else set()
        )
        non_terminal = [s for s, st in self.task_states.items() if st not in effectively_terminal]
        if non_terminal:
            return self._maybe_wait()

        merged = tuple(s for s, st in self.task_states.items() if st == DagTaskState.MERGED)
        failed = tuple(s for s, st in self.task_states.items() if st == DagTaskState.FAILED)
        skipped = tuple(s for s, st in self.task_states.items() if st == DagTaskState.SKIPPED)

        if failed or skipped:
            self.state = DagState.PARTIAL if merged else DagState.FAILED
        else:
            self.state = DagState.COMPLETED

        return [DagDone(status=self.state, merged=merged, failed=failed, skipped=skipped)]


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
    claim_violations: tuple[str, ...] = ()  # paths_written not in file_claims


def build_manifest_on_completion(
    project_root: str,
    slug: str,
    base_sha: str,
    file_claims: tuple[str, ...] = (),
) -> SemanticManifest:
    """Build a manifest from the worker's actual git diff after completion."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    paths_written = (
        tuple(f for f in result.stdout.strip().splitlines() if f) if result.returncode == 0 else ()
    )

    claim_set = set(file_claims)
    violations = tuple(p for p in paths_written if p not in claim_set) if claim_set else ()

    return SemanticManifest(
        base_sha=base_sha,
        file_claims=file_claims,
        paths_written=paths_written,
        claim_violations=violations,
    )


def validate_manifest_freshness(
    project_root: str,
    manifest: SemanticManifest,
) -> tuple[bool, list[str]]:
    """Check if main has changed files the worker wrote to since base_sha.

    Returns (is_fresh, stale_files). If stale_files is non-empty, the
    worker's changes may conflict with main.
    """
    import subprocess

    if not manifest.base_sha or not manifest.paths_written:
        return True, []

    result = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only", f"{manifest.base_sha}..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return True, []  # Can't check, assume fresh

    main_changed = set(f for f in result.stdout.strip().splitlines() if f)
    worker_written = set(manifest.paths_written)
    stale = sorted(main_changed & worker_written)
    return len(stale) == 0, stale


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
