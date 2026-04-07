"""Tests for src/dgov/actions.py."""

from dataclasses import FrozenInstanceError

import pytest

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
)


class TestConstruction:
    """Verify each dataclass can be instantiated with its fields."""

    def test_dispatch_task(self) -> None:
        action = DispatchTask(task_slug="a")
        assert action.task_slug == "a"

    def test_review_task(self) -> None:
        action = ReviewTask(task_slug="a", pane_slug="p")
        assert action.task_slug == "a"
        assert action.pane_slug == "p"

    def test_merge_task_with_file_claims(self) -> None:
        action = MergeTask(task_slug="a", pane_slug="p", file_claims=("f.py",))
        assert action.task_slug == "a"
        assert action.pane_slug == "p"
        assert action.file_claims == ("f.py",)

    def test_merge_task_default_file_claims(self) -> None:
        action = MergeTask(task_slug="a", pane_slug="p")
        assert action.file_claims == ()

    def test_dag_done(self) -> None:
        action = DagDone(
            status="success",
            merged=("task1",),
            failed=("task2",),
            skipped=("task3",),
            blocked=("task4",),
        )
        assert action.status == "success"
        assert action.merged == ("task1",)
        assert action.failed == ("task2",)
        assert action.skipped == ("task3",)
        assert action.blocked == ("task4",)

    def test_interrupt_governor(self) -> None:
        action = InterruptGovernor(task_slug="a", pane_slug="p", reason="timeout")
        assert action.task_slug == "a"
        assert action.pane_slug == "p"
        assert action.reason == "timeout"

    def test_task_dispatched(self) -> None:
        event = TaskDispatched(task_slug="a", pane_slug="p")
        assert event.task_slug == "a"
        assert event.pane_slug == "p"

    def test_task_wait_done(self) -> None:
        event = TaskWaitDone(task_slug="a", pane_slug="p", task_state="running")
        assert event.task_slug == "a"
        assert event.pane_slug == "p"
        assert event.task_state == "running"

    def test_task_review_done(self) -> None:
        event = TaskReviewDone(task_slug="a", passed=True, verdict="LGTM", commit_count=3)
        assert event.task_slug == "a"
        assert event.passed is True
        assert event.verdict == "LGTM"
        assert event.commit_count == 3

    def test_task_merge_done(self) -> None:
        event = TaskMergeDone(task_slug="a", error=None)
        assert event.task_slug == "a"
        assert event.error is None

    def test_task_governor_resumed(self) -> None:
        event = TaskGovernorResumed(task_slug="a", action=GovernorAction.RETRY)
        assert event.task_slug == "a"
        assert event.action == GovernorAction.RETRY


class TestFrozen:
    """Verify dataclasses are immutable."""

    def test_dispatch_task_is_frozen(self) -> None:
        action = DispatchTask(task_slug="a")
        with pytest.raises(FrozenInstanceError):
            action.task_slug = "b"  # type: ignore[misc]

    def test_task_merge_done_is_frozen(self) -> None:
        event = TaskMergeDone(task_slug="a")
        with pytest.raises(FrozenInstanceError):
            event.error = "new error"  # type: ignore[misc]


class TestGovernorAction:
    """Verify GovernorAction enum behavior."""

    def test_enum_values(self) -> None:
        assert GovernorAction.RETRY == "retry"
        assert GovernorAction.FAIL == "fail"
        assert GovernorAction.SKIP == "skip"

    def test_enum_members_accessible(self) -> None:
        assert GovernorAction.RETRY.value == "retry"
        assert GovernorAction.FAIL.value == "fail"
        assert GovernorAction.SKIP.value == "skip"

    def test_strenum_compares_with_strings(self) -> None:
        assert GovernorAction.RETRY == "retry"
        assert GovernorAction.FAIL == "fail"
        assert GovernorAction.SKIP == "skip"
        assert "retry" == GovernorAction.RETRY


class TestTypeUnions:
    """Verify DagAction and DagEvent Union types."""

    def test_dispatch_task_is_dag_action(self) -> None:
        action: DagAction = DispatchTask(task_slug="a")
        assert isinstance(action, DispatchTask)

    def test_review_task_is_dag_action(self) -> None:
        action: DagAction = ReviewTask(task_slug="a", pane_slug="p")
        assert isinstance(action, ReviewTask)

    def test_merge_task_is_dag_action(self) -> None:
        action: DagAction = MergeTask(task_slug="a", pane_slug="p")
        assert isinstance(action, MergeTask)

    def test_interrupt_governor_is_dag_action(self) -> None:
        action: DagAction = InterruptGovernor(task_slug="a", pane_slug="p", reason="timeout")
        assert isinstance(action, InterruptGovernor)

    def test_dag_done_is_dag_action(self) -> None:
        action: DagAction = DagDone(status="success", merged=(), failed=(), skipped=(), blocked=())
        assert isinstance(action, DagDone)

    def test_task_dispatched_is_dag_event(self) -> None:
        event: DagEvent = TaskDispatched(task_slug="a", pane_slug="p")
        assert isinstance(event, TaskDispatched)

    def test_task_wait_done_is_dag_event(self) -> None:
        event: DagEvent = TaskWaitDone(task_slug="a", pane_slug="p", task_state="done")
        assert isinstance(event, TaskWaitDone)

    def test_task_review_done_is_dag_event(self) -> None:
        event: DagEvent = TaskReviewDone(task_slug="a", passed=True, verdict="OK", commit_count=1)
        assert isinstance(event, TaskReviewDone)

    def test_task_merge_done_is_dag_event(self) -> None:
        event: DagEvent = TaskMergeDone(task_slug="a")
        assert isinstance(event, TaskMergeDone)

    def test_task_governor_resumed_is_dag_event(self) -> None:
        event: DagEvent = TaskGovernorResumed(task_slug="a", action=GovernorAction.RETRY)
        assert isinstance(event, TaskGovernorResumed)
