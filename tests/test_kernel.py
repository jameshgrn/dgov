"""Tests for the DagKernel pure state machine.

Every test: build kernel, feed events, assert state + actions.
No I/O, no mocking, no fixtures beyond plain dicts.
"""

from __future__ import annotations

import pytest

from dgov.actions import (
    DagDone,
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
from dgov.kernel import (
    DagKernel,
    DagState,
    TaskState,
    _topological_sort,
)
from dgov.types import TaskState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _k(deps: dict[str, tuple[str, ...]]) -> DagKernel:
    """Build a kernel from a dependency dict."""
    return DagKernel(deps=deps)


def _happy(kernel: DagKernel, slug: str, pane: str = "p") -> list:
    """Drive a single task through dispatch -> done -> review pass -> merge ok."""
    a = []
    a.extend(kernel.handle(TaskDispatched(slug, pane)))
    a.extend(kernel.handle(TaskWaitDone(slug, pane, TaskState.DONE)))
    a.extend(kernel.handle(TaskReviewDone(slug, passed=True, verdict="ok", commit_count=1)))
    a.extend(kernel.handle(TaskMergeDone(slug)))
    return a


def _types(actions: list) -> list[type]:
    return [type(a) for a in actions]


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


class TestTopoSort:
    def test_empty(self):
        assert _topological_sort({}) == []

    def test_single(self):
        assert _topological_sort({"a": ()}) == ["a"]

    def test_chain(self):
        order = _topological_sort({"a": (), "b": ("a",), "c": ("b",)})
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond(self):
        order = _topological_sort({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_cycle_raises(self):
        with pytest.raises(ValueError, match="Cycle"):
            _topological_sort({"a": ("b",), "b": ("a",)})

    def test_deterministic(self):
        deps = {"c": (), "a": (), "b": ("a",)}
        assert _topological_sort(deps) == _topological_sort(deps)


# ---------------------------------------------------------------------------
# Init + status
# ---------------------------------------------------------------------------


class TestInit:
    def test_all_pending(self):
        k = _k({"a": (), "b": ("a",)})
        assert all(st == TaskState.PENDING for st in k.task_states.values())

    def test_idle_before_start(self):
        k = _k({"a": ()})
        assert k.status == DagState.IDLE
        assert not k.done

    def test_merge_order_respects_deps(self):
        k = _k({"a": (), "b": ("a",), "c": ("b",)})
        assert k.merge_order.index("a") < k.merge_order.index("b")
        assert k.merge_order.index("b") < k.merge_order.index("c")


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


class TestStart:
    def test_dispatches_roots(self):
        k = _k({"a": (), "b": ("a",)})
        actions = k.start()
        slugs = [a.task_slug for a in actions if isinstance(a, DispatchTask)]
        assert slugs == ["a"]

    def test_dispatches_all_independent(self):
        k = _k({"a": (), "b": (), "c": ()})
        actions = k.start()
        slugs = sorted(a.task_slug for a in actions if isinstance(a, DispatchTask))
        assert slugs == ["a", "b", "c"]

    def test_blocked_not_dispatched(self):
        k = _k({"a": (), "b": ("a",)})
        actions = k.start()
        assert not any(isinstance(a, DispatchTask) and a.task_slug == "b" for a in actions)


# ---------------------------------------------------------------------------
# Single task happy path
# ---------------------------------------------------------------------------


class TestSingleHappy:
    def test_full_lifecycle(self):
        k = _k({"a": ()})
        k.start()

        actions = k.handle(TaskDispatched("a", "p-a"))
        assert k.task_states["a"] == TaskState.ACTIVE
        assert actions == []  # dispatched is ack-only, no action needed

        actions = k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        assert k.task_states["a"] == TaskState.REVIEWING
        assert any(isinstance(a, ReviewTask) for a in actions)

        actions = k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        assert k.task_states["a"] == TaskState.MERGING
        assert any(isinstance(a, MergeTask) for a in actions)

        actions = k.handle(TaskMergeDone("a"))
        assert k.task_states["a"] == TaskState.MERGED
        assert k.done
        assert k.status == DagState.COMPLETED

    def test_emits_dag_done(self):
        k = _k({"a": ()})
        k.start()
        actions = _happy(k, "a")
        dones = [a for a in actions if isinstance(a, DagDone)]
        assert len(dones) == 1
        assert dones[0].merged == ("a",)
        assert dones[0].failed == ()
        assert dones[0].skipped == ()


# ---------------------------------------------------------------------------
# Chain (a -> b -> c)
# ---------------------------------------------------------------------------


class TestChain:
    def test_sequential_unblock(self):
        k = _k({"a": (), "b": ("a",), "c": ("b",)})
        k.start()
        actions = _happy(k, "a")
        assert any(isinstance(a, DispatchTask) and a.task_slug == "b" for a in actions)

    def test_full_chain(self):
        k = _k({"a": (), "b": ("a",), "c": ("b",)})
        k.start()
        for slug in ["a", "b", "c"]:
            _happy(k, slug, f"p-{slug}")
        assert k.status == DagState.COMPLETED
        assert all(st == TaskState.MERGED for st in k.task_states.values())


# ---------------------------------------------------------------------------
# Parallel tasks
# ---------------------------------------------------------------------------


class TestParallel:
    def test_both_dispatched(self):
        k = _k({"a": (), "b": ()})
        actions = k.start()
        slugs = sorted(a.task_slug for a in actions if isinstance(a, DispatchTask))
        assert slugs == ["a", "b"]

    def test_serial_merge(self):
        """Both MERGE_READY, but only one merges at a time."""
        k = _k({"a": (), "b": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))

        merging = [s for s, st in k.task_states.items() if st == TaskState.MERGING]
        ready = [s for s, st in k.task_states.items() if st == TaskState.REVIEWED_PASS]
        assert len(merging) == 1
        assert len(ready) == 1

    def test_second_merges_after_first(self):
        k = _k({"a": (), "b": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))

        first = [s for s, st in k.task_states.items() if st == TaskState.MERGING][0]
        k.handle(TaskMergeDone(first))
        merging = [s for s, st in k.task_states.items() if st == TaskState.MERGING]
        assert len(merging) == 1
        assert merging[0] != first


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailure:
    def test_worker_fail_interrupts(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        actions = k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        assert any(isinstance(a, InterruptGovernor) for a in actions)

    def test_review_fail(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=False, verdict="bad", commit_count=0))
        assert k.task_states["a"] == TaskState.FAILED

    def test_merge_error(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        k.handle(TaskMergeDone("a", error="conflict"))
        assert k.task_states["a"] == TaskState.FAILED

    def test_all_failed(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=False, verdict="bad", commit_count=0))
        assert k.status == DagState.FAILED

    def test_partial(self):
        k = _k({"a": (), "b": ()})
        k.start()
        _happy(k, "a", "p-a")
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        k.handle(TaskReviewDone("b", passed=False, verdict="bad", commit_count=0))
        assert k.status == DagState.PARTIAL


# ---------------------------------------------------------------------------
# Merge scan: failure doesn't block (the cursor bug fix)
# ---------------------------------------------------------------------------


class TestMergeScanSkipsFailures:
    def test_first_fails_second_still_merges(self):
        """The old cursor bug: first task fails review, second is stuck forever.
        Scan-based merge skips terminal states."""
        k = _k({"a": (), "b": ()})
        k.start()

        # Both dispatch and complete
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))

        # a fails review (first in topo order)
        k.handle(TaskReviewDone("a", passed=False, verdict="bad", commit_count=0))
        assert k.task_states["a"] == TaskState.FAILED

        # b passes review — should be MERGE_READY and immediately start merging
        actions = k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))
        assert k.task_states["b"] == TaskState.MERGING
        assert any(isinstance(a, MergeTask) and a.task_slug == "b" for a in actions)

    def test_first_merge_error_second_proceeds(self):
        """First task merge fails, second should still merge."""
        k = _k({"a": (), "b": ()})
        k.start()

        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        # a starts merging (first in order)
        assert k.task_states["a"] == TaskState.MERGING

        k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))
        # b is MERGE_READY but blocked by a's in-progress merge
        assert k.task_states["b"] == TaskState.REVIEWED_PASS

        # a merge fails
        k.handle(TaskMergeDone("a", error="conflict"))
        assert k.task_states["a"] == TaskState.FAILED
        # b should now be merging
        assert k.task_states["b"] == TaskState.MERGING

    def test_skip_also_unblocks(self):
        """Skipped task doesn't block merge scan."""
        k = _k({"a": (), "b": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))

        # Governor skips a
        k.handle(TaskGovernorResumed("a", GovernorAction.SKIP))
        assert k.task_states["a"] == TaskState.SKIPPED

        # b completes and should merge without being blocked
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))
        assert k.task_states["b"] == TaskState.MERGING

    def test_in_progress_task_still_blocks(self):
        """Non-terminal task in topo order blocks merge of later task."""
        k = _k({"a": (), "b": ()})
        k.start()

        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskDispatched("b", "p-b"))
        # a still WAITING, b completes
        k.handle(TaskWaitDone("b", "p-b", TaskState.DONE))
        actions = k.handle(TaskReviewDone("b", passed=True, verdict="ok", commit_count=1))

        # b is MERGE_READY but a is WAITING (non-terminal), so b can't merge yet
        assert k.task_states["b"] == TaskState.REVIEWED_PASS
        assert not any(isinstance(a, MergeTask) for a in actions)


# ---------------------------------------------------------------------------
# Governor retry / fail / skip
# ---------------------------------------------------------------------------


class TestGovernorResume:
    def test_retry_resets_and_redispatches(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        actions = k.handle(TaskGovernorResumed("a", GovernorAction.RETRY))
        assert k.task_states["a"] == TaskState.PENDING
        assert k.attempts["a"] == 1
        assert any(isinstance(a, DispatchTask) and a.task_slug == "a" for a in actions)

    def test_fail_terminal(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        k.handle(TaskGovernorResumed("a", GovernorAction.FAIL))
        assert k.task_states["a"] == TaskState.FAILED
        assert k.done

    def test_skip_terminal(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        k.handle(TaskGovernorResumed("a", GovernorAction.SKIP))
        assert k.task_states["a"] == TaskState.SKIPPED
        assert k.done

    def test_skip_in_dag_done(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        actions = k.handle(TaskGovernorResumed("a", GovernorAction.SKIP))
        dones = [a for a in actions if isinstance(a, DagDone)]
        assert len(dones) == 1
        assert dones[0].skipped == ("a",)

    def test_retry_increments(self):
        k = _k({"a": ()})
        k.start()
        for i in range(3):
            k.handle(TaskDispatched("a", f"p-{i}"))
            k.handle(TaskWaitDone("a", f"p-{i}", TaskState.FAILED))
            k.handle(TaskGovernorResumed("a", GovernorAction.RETRY))
            assert k.attempts["a"] == i + 1

    def test_governor_resumed_ignored_for_merged(self):
        """Cannot un-merge a task via governor retry."""
        k = _k({"a": ()})
        k.start()
        _happy(k, "a")
        assert k.task_states["a"] == TaskState.MERGED
        k.handle(TaskGovernorResumed("a", GovernorAction.RETRY))
        # State unchanged — guard prevented revert
        assert k.task_states["a"] == TaskState.MERGED

    def test_governor_resumed_ignored_for_reviewing(self):
        """Cannot interrupt a task that's being reviewed."""
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        assert k.task_states["a"] == TaskState.REVIEWING
        actions = k.handle(TaskGovernorResumed("a", GovernorAction.RETRY))
        assert actions == []
        assert k.task_states["a"] == TaskState.REVIEWING

    def test_skip_unblocks_dependent(self):
        """Skipping a task does NOT satisfy its dependents (only MERGED does)."""
        k = _k({"a": (), "b": ("a",)})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.FAILED))
        actions = k.handle(TaskGovernorResumed("a", GovernorAction.SKIP))
        # b depends on a. a is SKIPPED, not MERGED. b should NOT dispatch.
        assert not any(isinstance(a, DispatchTask) and a.task_slug == "b" for a in actions)


# ---------------------------------------------------------------------------
# Dependency gating
# ---------------------------------------------------------------------------


class TestDeps:
    def test_blocked_not_dispatched(self):
        k = _k({"a": (), "b": ("a",)})
        actions = k.start()
        assert {a.task_slug for a in actions if isinstance(a, DispatchTask)} == {"a"}

    def test_unblocked_after_merge(self):
        k = _k({"a": (), "b": ("a",)})
        k.start()
        actions = _happy(k, "a")
        assert any(isinstance(a, DispatchTask) and a.task_slug == "b" for a in actions)

    def test_dep_failure_blocks_forever(self):
        k = _k({"a": (), "b": ("a",)})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        k.handle(TaskReviewDone("a", passed=False, verdict="bad", commit_count=0))
        assert not any(isinstance(a, DispatchTask) for a in k._schedule())

    def test_diamond(self):
        k = _k({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
        k.start()
        _happy(k, "a", "p-a")
        dispatched = {a.task_slug for a in k._schedule()}
        assert dispatched == {"b", "c"}

        _happy(k, "b", "p-b")
        _happy(k, "c", "p-c")
        dispatched = {a.task_slug for a in k._schedule()}
        assert "d" in dispatched


# ---------------------------------------------------------------------------
# Guard rails / idempotency
# ---------------------------------------------------------------------------


class TestGuardRails:
    def test_duplicate_dispatch_ignored(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        actions = k.handle(TaskDispatched("a", "p-a-2"))
        assert actions == []

    def test_wait_done_wrong_state_ignored(self):
        k = _k({"a": ()})
        k.start()
        actions = k.handle(TaskWaitDone("a", "p-a", TaskState.DONE))
        assert actions == []

    def test_review_wrong_state_ignored(self):
        k = _k({"a": ()})
        k.start()
        actions = k.handle(TaskReviewDone("a", passed=True, verdict="ok", commit_count=1))
        assert actions == []

    def test_merge_wrong_state_ignored(self):
        k = _k({"a": ()})
        k.start()
        actions = k.handle(TaskMergeDone("a"))
        assert actions == []

    def test_unknown_slug_safe(self):
        k = _k({"a": ()})
        k.start()
        actions = k.handle(TaskDispatched("ghost", "p-x"))
        assert actions == []

    def test_unknown_event_type_safe(self):
        """Event type not in dispatch table returns empty."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeEvent:
            task_slug: str

        k = _k({"a": ()})
        actions = k.handle(FakeEvent("a"))
        assert actions == []


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


class TestDispatchTable:
    def test_all_handled_events_in_table(self):
        """Every event handler has a corresponding dispatch entry."""
        from dgov.kernel import _DISPATCH

        k = _k({"a": ()})
        for method_name in _DISPATCH.values():
            assert hasattr(k, method_name), f"Missing handler: {method_name}"

    def test_dispatch_is_dict_not_getattr_regex(self):
        """Dispatch table is a plain dict — no string mangling."""
        from dgov.kernel import _DISPATCH

        assert isinstance(_DISPATCH, dict)
        for key in _DISPATCH:
            assert isinstance(key, type), f"Key should be a type, got {key}"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_structure(self):
        k = _k({"a": (), "b": ("a",)})
        d = k.to_dict()
        assert set(d.keys()) == {"task_states", "pane_slugs", "attempts", "merge_order"}
        assert d["task_states"]["a"] == "pending"

    def test_after_progress(self):
        k = _k({"a": ()})
        k.start()
        k.handle(TaskDispatched("a", "p-a"))
        d = k.to_dict()
        assert d["task_states"]["a"] == "active"
        assert d["pane_slugs"]["a"] == "p-a"
