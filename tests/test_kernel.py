from __future__ import annotations

import pytest

from dgov.executor import CleanupOnlyResult, MergeOnlyResult, ReviewOnlyResult, WaitOnlyResult
from dgov.kernel import (
    CleanupCompleted,
    CleanupPane,
    KernelState,
    MergeCompleted,
    MergePane,
    PostDispatchKernel,
    ReviewCompleted,
    ReviewPane,
    WaitCompleted,
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
