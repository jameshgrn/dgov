"""Tests for dgov.kernel — state machine logic."""

from __future__ import annotations

import pytest

from dgov.kernel import (
    DagDone,
    DagKernel,
    DagState,
    DagTaskState,
    DispatchTask,
    InterruptGovernor,
    MergeTask,
    RetryTask,
    ReviewTask,
    SkipTask,
    TaskClosed,
    TaskDispatched,
    TaskDispatchFailed,
    TaskGovernorResumed,
    TaskMergeDone,
    TaskRetryStarted,
    TaskReviewDone,
    TaskWaitDone,
    WaitForAny,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# DagKernel tests
# ---------------------------------------------------------------------------


def _simple_dag() -> dict[str, tuple[str, ...]]:
    """a -> b -> c (linear chain)."""
    return {"a": (), "b": ("a",), "c": ("b",)}


def _diamond_dag() -> dict[str, tuple[str, ...]]:
    """a -> b, a -> c, b -> d, c -> d (diamond)."""
    return {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")}


def _parallel_dag() -> dict[str, tuple[str, ...]]:
    """a, b, c — no dependencies."""
    return {"a": (), "b": (), "c": ()}


def test_dag_kernel_starts_by_dispatching_ready_tasks() -> None:
    kernel = DagKernel(deps=_simple_dag())
    actions = kernel.start()

    assert kernel.state == DagState.RUNNING
    # Only 'a' has no deps
    assert actions == [DispatchTask("a")]
    assert kernel.task_states["a"] == DagTaskState.DISPATCHED
    assert kernel.task_states["b"] == DagTaskState.PENDING
    assert kernel.task_states["c"] == DagTaskState.PENDING


def test_dag_kernel_parallel_dispatches_all_independent() -> None:
    kernel = DagKernel(deps=_parallel_dag())
    actions = kernel.start()

    dispatch_slugs = {a.task_slug for a in actions if isinstance(a, DispatchTask)}
    assert dispatch_slugs == {"a", "b", "c"}


def test_dag_kernel_respects_concurrency_limit() -> None:
    kernel = DagKernel(deps=_parallel_dag(), max_concurrent=2)
    actions = kernel.start()

    dispatches = [a for a in actions if isinstance(a, DispatchTask)]
    assert len(dispatches) == 2


def test_dag_kernel_dispatch_then_wait() -> None:
    kernel = DagKernel(deps=_simple_dag())
    kernel.start()

    actions = kernel.handle(TaskDispatched("a", "pane-a"))

    assert kernel.task_states["a"] == DagTaskState.WAITING
    assert kernel.pane_slugs["a"] == "pane-a"
    waits = [a for a in actions if isinstance(a, WaitForAny)]
    assert len(waits) == 1
    assert "a" in waits[0].task_slugs


def test_dag_kernel_wait_done_triggers_review() -> None:
    kernel = DagKernel(deps=_simple_dag())
    kernel.start()
    kernel.handle(TaskDispatched("a", "pane-a"))

    actions = kernel.handle(TaskWaitDone("a", "pane-a", "done"))

    assert kernel.task_states["a"] == DagTaskState.REVIEWING
    reviews = [a for a in actions if isinstance(a, ReviewTask)]
    assert len(reviews) == 1
    assert reviews[0].task_slug == "a"


def test_dag_kernel_review_pass_enables_merge() -> None:
    kernel = DagKernel(deps=_simple_dag(), auto_merge=True)
    kernel.start()
    kernel.handle(TaskDispatched("a", "pane-a"))
    kernel.handle(TaskWaitDone("a", "pane-a", "done"))

    actions = kernel.handle(TaskReviewDone("a", passed=True, verdict="safe", commit_count=1))

    assert kernel.task_states["a"] == DagTaskState.MERGING
    merges = [a for a in actions if isinstance(a, MergeTask)]
    assert len(merges) == 1


def test_dag_kernel_merge_success_unblocks_dependents() -> None:
    kernel = DagKernel(deps=_simple_dag(), auto_merge=True)
    kernel.start()
    kernel.handle(TaskDispatched("a", "pane-a"))
    kernel.handle(TaskWaitDone("a", "pane-a", "done"))
    kernel.handle(TaskReviewDone("a", passed=True, verdict="safe", commit_count=1))

    actions = kernel.handle(TaskMergeDone("a"))

    assert kernel.task_states["a"] == DagTaskState.MERGED
    # 'b' should now be dispatched since its dep 'a' is merged
    dispatches = [a for a in actions if isinstance(a, DispatchTask)]
    assert any(d.task_slug == "b" for d in dispatches)


def test_dag_kernel_failure_skips_dependents() -> None:
    kernel = DagKernel(deps=_simple_dag())
    kernel.start()
    kernel.handle(TaskDispatched("a", "pane-a"))

    # Fail attempt 1, 2, 3, then fail task (default max_retries=3)
    # Actually, default is 3, so attempts 1, 2, 3 are retries.
    # 1st attempt fails -> RetryTask(attempt=1)
    # TaskRetryStarted(attempt=1) -> attempts=2
    # 2nd attempt fails -> RetryTask(attempt=2)
    # TaskRetryStarted(attempt=2) -> attempts=3
    # 3rd attempt fails -> RetryTask(attempt=3)
    # TaskRetryStarted(attempt=3) -> attempts=4
    # 4th attempt fails -> BLOCKED_ON_GOVERNOR (new behavior)

    for i in range(1, 4):
        actions = kernel.handle(TaskWaitDone("a", f"pane-{i}", "failed"))
        assert isinstance(actions[0], RetryTask)
        assert actions[0].attempt == i
        kernel.handle(TaskRetryStarted("a", f"pane-{i + 1}", attempt=i))

    actions = kernel.handle(TaskWaitDone("a", "pane-4", "failed"))
    assert kernel.task_states["a"] == DagTaskState.BLOCKED_ON_GOVERNOR
    assert any(isinstance(a, InterruptGovernor) for a in actions)


def test_dag_kernel_diamond_parallel_execution() -> None:
    kernel = DagKernel(deps=_diamond_dag(), auto_merge=True)
    kernel.start()

    # Only 'a' dispatched initially
    assert kernel.task_states["a"] == DagTaskState.DISPATCHED

    # Complete 'a' fully
    kernel.handle(TaskDispatched("a", "pane-a"))
    kernel.handle(TaskWaitDone("a", "pane-a", "done"))
    kernel.handle(TaskReviewDone("a", passed=True, verdict="safe", commit_count=1))
    actions = kernel.handle(TaskMergeDone("a"))
    kernel.handle(TaskClosed("a"))

    # 'b' and 'c' should both be dispatched now
    dispatches = {a.task_slug for a in actions if isinstance(a, DispatchTask)}
    assert dispatches == {"b", "c"}


def test_dag_kernel_full_linear_lifecycle() -> None:
    """Drive a→b→c through full lifecycle to DagDone."""
    kernel = DagKernel(deps=_simple_dag(), auto_merge=True)
    collected: list = list(kernel.start())

    for slug in ["a", "b", "c"]:
        pane = f"pane-{slug}"
        collected.extend(kernel.handle(TaskDispatched(slug, pane)))
        collected.extend(kernel.handle(TaskWaitDone(slug, pane, "done")))
        collected.extend(kernel.handle(TaskReviewDone(slug, True, "safe", 1)))
        collected.extend(kernel.handle(TaskMergeDone(slug)))
        collected.extend(kernel.handle(TaskClosed(slug)))

    dones = [a for a in collected if isinstance(a, DagDone)]
    assert len(dones) == 1
    assert dones[0].status == DagState.COMPLETED
    assert set(dones[0].merged) == {"a", "b", "c"}
    assert kernel.done


def test_dag_kernel_dispatch_failure_skips_dependents() -> None:
    kernel = DagKernel(deps=_simple_dag())
    kernel.start()

    actions = kernel.handle(TaskDispatchFailed("a", "agent down"))

    assert kernel.task_states["a"] == DagTaskState.FAILED
    skips = {a.task_slug for a in actions if isinstance(a, SkipTask)}
    assert skips == {"b", "c"}

    dones = [a for a in actions if isinstance(a, DagDone)]
    assert len(dones) == 1
    assert dones[0].status == DagState.FAILED


def test_dag_kernel_merge_serialization_follows_topo_order() -> None:
    """In diamond dag, b and c run in parallel but merge in topo order."""
    kernel = DagKernel(deps=_diamond_dag(), auto_merge=True)
    kernel.start()

    # Complete 'a'
    kernel.handle(TaskDispatched("a", "pane-a"))
    kernel.handle(TaskWaitDone("a", "pane-a", "done"))
    kernel.handle(TaskReviewDone("a", True, "safe", 1))
    kernel.handle(TaskMergeDone("a"))
    actions = kernel.handle(TaskClosed("a"))

    # Dispatch and review both b and c
    kernel.handle(TaskDispatched("b", "pane-b"))
    kernel.handle(TaskDispatched("c", "pane-c"))
    kernel.handle(TaskWaitDone("b", "pane-b", "done"))
    kernel.handle(TaskWaitDone("c", "pane-c", "done"))

    # Review c first (out of topo order)
    kernel.handle(TaskReviewDone("c", True, "safe", 1))
    # c is merge_ready but b comes first in topo order
    assert kernel.task_states["c"] == DagTaskState.MERGE_READY

    # Review b
    actions = kernel.handle(TaskReviewDone("b", True, "safe", 1))
    # b should start merging (it's first in topo order)
    merges = [a for a in actions if isinstance(a, MergeTask)]
    assert any(m.task_slug == "b" for m in merges)


def test_dag_kernel_pre_skip_propagates() -> None:
    """Pre-skipped tasks propagate to dependents at init."""
    kernel = DagKernel(deps=_simple_dag(), skip=frozenset({"a"}))
    assert kernel.task_states["a"] == DagTaskState.SKIPPED
    assert kernel.task_states["b"] == DagTaskState.SKIPPED
    assert kernel.task_states["c"] == DagTaskState.SKIPPED

    actions = kernel.start()
    dones = [a for a in actions if isinstance(a, DagDone)]
    assert len(dones) == 1
    assert dones[0].status == DagState.FAILED


def test_dag_kernel_partial_skip() -> None:
    """Skip one leaf in diamond — others still run."""
    kernel = DagKernel(deps=_diamond_dag(), skip=frozenset({"b"}))
    assert kernel.task_states["a"] == DagTaskState.PENDING
    assert kernel.task_states["b"] == DagTaskState.SKIPPED
    assert kernel.task_states["c"] == DagTaskState.PENDING
    # d depends on b → skipped
    assert kernel.task_states["d"] == DagTaskState.SKIPPED


def test_kernel_interrupts_governor_on_exhausted_retries() -> None:
    deps = {"a": ()}
    # Set max_retries to 1, so 1st failure retries, 2nd failure interrupts
    kernel = DagKernel(deps=deps, max_retries=1)

    kernel.start()
    kernel.handle(TaskDispatched("a", "pane-1"))
    kernel.handle(TaskWaitDone("a", "pane-1", "done"))

    # 1. First review failure -> Retry
    actions = kernel.handle(
        TaskReviewDone("a", passed=False, verdict="unreliable", commit_count=0)
    )
    assert isinstance(actions[0], RetryTask)

    kernel.handle(TaskRetryStarted("a", "pane-2", attempt=1))
    kernel.handle(TaskWaitDone("a", "pane-2", "done"))

    # 2. Second review failure -> Interrupt
    actions = kernel.handle(
        TaskReviewDone("a", passed=False, verdict="unreliable", commit_count=0)
    )
    assert kernel.task_states["a"] == DagTaskState.BLOCKED_ON_GOVERNOR
    assert any(isinstance(a, InterruptGovernor) for a in actions)
    assert any(
        a.task_slug == "a" and a.reason == "review_failed"
        for a in actions
        if isinstance(a, InterruptGovernor)
    )


def test_kernel_governor_resume_retry() -> None:
    deps = {"a": ()}
    kernel = DagKernel(deps=deps)
    kernel.task_states["a"] = DagTaskState.BLOCKED_ON_GOVERNOR

    # Resume with retry
    actions = kernel.handle(TaskGovernorResumed("a", action="retry"))
    assert kernel.task_states["a"] == DagTaskState.DISPATCHED
    assert any(isinstance(a, DispatchTask) and a.task_slug == "a" for a in actions)
