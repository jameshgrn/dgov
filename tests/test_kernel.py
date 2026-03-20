from __future__ import annotations

import pytest

from dgov.executor import CleanupOnlyResult, MergeOnlyResult, ReviewOnlyResult, WaitOnlyResult
from dgov.kernel import (
    CleanupCompleted,
    CleanupPane,
    DagDone,
    DagKernel,
    DagState,
    DagTaskState,
    DispatchTask,
    KernelState,
    MergeCompleted,
    MergePane,
    MergeTask,
    PostDispatchKernel,
    ReviewCompleted,
    ReviewPane,
    ReviewTask,
    SkipTask,
    TaskClosed,
    TaskDispatched,
    TaskDispatchFailed,
    TaskMergeDone,
    TaskReviewDone,
    TaskWaitDone,
    WaitCompleted,
    WaitForAny,
    WaitForPane,
)

pytestmark = pytest.mark.unit


def test_kernel_starts_with_wait_action() -> None:
    kernel = PostDispatchKernel(auto_merge=True)

    actions = kernel.start("task")

    assert kernel.state is KernelState.WAITING
    assert actions == [WaitForPane("task")]


def test_kernel_routes_successful_wait_to_review() -> None:
    kernel = PostDispatchKernel(auto_merge=True)
    kernel.start("task")

    actions = kernel.handle(
        WaitCompleted(WaitOnlyResult(state="completed", slug="task", pane_state="done"))
    )

    assert kernel.state is KernelState.REVIEWING
    assert actions == [ReviewPane("task")]


def test_kernel_routes_safe_review_to_merge_when_auto_merge_enabled() -> None:
    kernel = PostDispatchKernel(auto_merge=True)
    kernel.start("task")
    kernel.handle(WaitCompleted(WaitOnlyResult(state="completed", slug="task", pane_state="done")))

    actions = kernel.handle(
        ReviewCompleted(
            ReviewOnlyResult(
                slug="task",
                review={"slug": "task", "verdict": "safe", "commit_count": 1},
                passed=True,
                verdict="safe",
                commit_count=1,
            )
        )
    )

    assert kernel.state is KernelState.MERGING
    assert actions == [MergePane("task")]


def test_kernel_routes_safe_review_to_cleanup_when_auto_merge_disabled() -> None:
    kernel = PostDispatchKernel(auto_merge=False)
    kernel.start("task")
    kernel.handle(WaitCompleted(WaitOnlyResult(state="completed", slug="task", pane_state="done")))

    actions = kernel.handle(
        ReviewCompleted(
            ReviewOnlyResult(
                slug="task",
                review={"slug": "task", "verdict": "safe", "commit_count": 1},
                passed=True,
                verdict="safe",
                commit_count=1,
            )
        )
    )

    assert kernel.state is KernelState.REVIEWED_PASS
    assert actions == [CleanupPane("task", state="review_pending", failure_stage=None)]


def test_kernel_routes_merge_success_to_completion_cleanup() -> None:
    kernel = PostDispatchKernel(auto_merge=True)
    kernel.start("task")
    kernel.handle(WaitCompleted(WaitOnlyResult(state="completed", slug="task", pane_state="done")))
    kernel.handle(
        ReviewCompleted(
            ReviewOnlyResult(
                slug="task",
                review={"slug": "task", "verdict": "safe", "commit_count": 1},
                passed=True,
                verdict="safe",
                commit_count=1,
            )
        )
    )

    actions = kernel.handle(
        MergeCompleted(MergeOnlyResult(slug="task", merge_result={"merged": "task"}))
    )

    assert kernel.state is KernelState.COMPLETED
    assert actions == [CleanupPane("task", state="completed", failure_stage=None)]


def test_kernel_terminal_cleanup_returns_no_actions() -> None:
    kernel = PostDispatchKernel(auto_merge=False)
    kernel.start("task")
    kernel.handle(WaitCompleted(WaitOnlyResult(state="completed", slug="task", pane_state="done")))
    kernel.handle(
        ReviewCompleted(
            ReviewOnlyResult(
                slug="task",
                review={"slug": "task", "verdict": "safe", "commit_count": 1},
                passed=True,
                verdict="safe",
                commit_count=1,
            )
        )
    )

    actions = kernel.handle(
        CleanupCompleted(
            CleanupOnlyResult(slug="task", action="preserve", reason="review_pending")
        )
    )

    assert kernel.state is KernelState.REVIEWED_PASS
    assert actions == []


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

    actions = kernel.handle(TaskWaitDone("a", "pane-a", "failed"))

    assert kernel.task_states["a"] == DagTaskState.FAILED
    skips = [a for a in actions if isinstance(a, SkipTask)]
    skip_slugs = {s.task_slug for s in skips}
    assert skip_slugs == {"b", "c"}
    assert kernel.task_states["b"] == DagTaskState.SKIPPED
    assert kernel.task_states["c"] == DagTaskState.SKIPPED


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
    kernel.handle(TaskClosed("a"))

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
