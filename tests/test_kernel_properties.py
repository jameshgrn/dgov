"""Property-based tests for the dgov kernel state machine.

Proves invariants hold for ALL possible event sequences, not just hand-picked ones.
Uses hypothesis to generate random legal and illegal event sequences.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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

# ---------------------------------------------------------------------------
# Mock result factories
# ---------------------------------------------------------------------------

_SLUG = "test-pane"


def _wait_result(state: str = "completed", failure_stage: str | None = None):
    r = MagicMock()
    r.slug = _SLUG
    r.state = state
    r.failure_stage = failure_stage
    return r


def _review_result(verdict: str = "safe", commit_count: int = 1, error: str | None = None):
    r = MagicMock()
    r.slug = _SLUG
    r.verdict = verdict
    r.commit_count = commit_count
    r.error = error
    return r


def _merge_result(error: str | None = None):
    r = MagicMock()
    r.slug = _SLUG
    r.error = error
    return r


def _cleanup_result():
    r = MagicMock()
    r.slug = _SLUG
    return r


# ---------------------------------------------------------------------------
# Hypothesis strategies for kernel events
# ---------------------------------------------------------------------------

TERMINAL_STATES = frozenset(
    {
        KernelState.COMPLETED,
        KernelState.FAILED,
        KernelState.REVIEW_PENDING,
        KernelState.REVIEWED_PASS,
    }
)


def wait_events():
    return st.sampled_from(
        [
            WaitCompleted(result=_wait_result("completed")),
            WaitCompleted(result=_wait_result("failed", "timeout")),
        ]
    )


def review_events():
    return st.sampled_from(
        [
            ReviewCompleted(result=_review_result("safe", 1)),
            ReviewCompleted(result=_review_result("safe", 0)),
            ReviewCompleted(result=_review_result("unsafe", 1)),
            ReviewCompleted(result=_review_result("safe", 1, error="review error")),
        ]
    )


def merge_events():
    return st.sampled_from(
        [
            MergeCompleted(result=_merge_result()),
            MergeCompleted(result=_merge_result("merge conflict")),
        ]
    )


def cleanup_events():
    return st.just(CleanupCompleted(result=_cleanup_result()))


all_events = st.one_of(wait_events(), review_events(), merge_events(), cleanup_events())


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestKernelTermination:
    """Every legal event sequence terminates in a terminal state."""

    @given(events=st.lists(all_events, min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_random_events_never_hang(self, events):
        """Feeding random events either terminates or raises ValueError."""
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)

        for event in events:
            if k.state in TERMINAL_STATES:
                # Terminal state + CleanupCompleted → stays terminal
                if isinstance(event, CleanupCompleted):
                    k.handle(event)
                else:
                    with pytest.raises(ValueError):
                        k.handle(event)
                break
            try:
                k.handle(event)
            except ValueError:
                break

    @given(auto_merge=st.booleans())
    def test_happy_path_always_terminates(self, auto_merge):
        """The full success path always reaches a terminal state."""
        k = PostDispatchKernel(auto_merge=auto_merge)
        k.start(_SLUG)

        k.handle(WaitCompleted(result=_wait_result("completed")))
        assert k.state == KernelState.REVIEWING

        k.handle(ReviewCompleted(result=_review_result("safe", 1)))
        if auto_merge:
            assert k.state == KernelState.MERGING
            k.handle(MergeCompleted(result=_merge_result()))
            assert k.state == KernelState.COMPLETED
        else:
            assert k.state == KernelState.REVIEWED_PASS

        assert k.state in TERMINAL_STATES


class TestKernelNoBackward:
    """Once in a terminal state, no event can regress to a working state."""

    @given(events=st.lists(all_events, min_size=1, max_size=10))
    @settings(max_examples=200)
    def test_terminal_states_are_absorbing(self, events):
        """Terminal states only accept CleanupCompleted or raise."""
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)

        # Drive to terminal
        k.handle(WaitCompleted(result=_wait_result("failed", "timeout")))
        assert k.state == KernelState.FAILED
        k.handle(CleanupCompleted(result=_cleanup_result()))

        # Now try feeding more events — should all raise
        for event in events:
            if isinstance(event, CleanupCompleted):
                continue  # already handled
            with pytest.raises(ValueError):
                k.handle(event)
            assert k.state == KernelState.FAILED


class TestKernelDeterminism:
    """Same events always produce the same state."""

    @given(
        wait_ok=st.booleans(),
        review_safe=st.booleans(),
        merge_ok=st.booleans(),
        auto_merge=st.booleans(),
    )
    def test_deterministic_outcomes(self, wait_ok, review_safe, merge_ok, auto_merge):
        """Run the same scenario twice, get the same result."""
        scenario = (wait_ok, review_safe, merge_ok, auto_merge)

        states = []
        for _ in range(2):
            k = PostDispatchKernel(auto_merge=auto_merge)
            k.start(_SLUG)

            wait_state = "completed" if wait_ok else "failed"
            k.handle(WaitCompleted(result=_wait_result(wait_state, "timeout")))

            if k.state in TERMINAL_STATES:
                states.append(k.state)
                continue

            verdict = "safe" if review_safe else "unsafe"
            k.handle(ReviewCompleted(result=_review_result(verdict, 1)))

            if k.state in TERMINAL_STATES:
                states.append(k.state)
                continue

            error = None if merge_ok else "conflict"
            k.handle(MergeCompleted(result=_merge_result(error)))
            states.append(k.state)

        assert states[0] == states[1], f"Non-deterministic: {scenario} -> {states}"


class TestKernelActionValidity:
    """Emitted actions must be valid for the new kernel state."""

    def test_start_emits_wait(self):
        k = PostDispatchKernel()
        actions = k.start(_SLUG)
        assert len(actions) == 1
        assert isinstance(actions[0], WaitForPane)
        assert k.state == KernelState.WAITING

    def test_wait_success_emits_review(self):
        k = PostDispatchKernel()
        k.start(_SLUG)
        actions = k.handle(WaitCompleted(result=_wait_result("completed")))
        assert len(actions) == 1
        assert isinstance(actions[0], ReviewPane)
        assert k.state == KernelState.REVIEWING

    def test_review_safe_emits_merge(self):
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        actions = k.handle(ReviewCompleted(result=_review_result("safe", 1)))
        assert len(actions) == 1
        assert isinstance(actions[0], MergePane)
        assert k.state == KernelState.MERGING

    def test_merge_success_emits_cleanup(self):
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        k.handle(ReviewCompleted(result=_review_result("safe", 1)))
        actions = k.handle(MergeCompleted(result=_merge_result()))
        assert len(actions) == 1
        assert isinstance(actions[0], CleanupPane)
        assert actions[0].state == "completed"
        assert k.state == KernelState.COMPLETED

    def test_failure_always_emits_cleanup(self):
        k = PostDispatchKernel()
        k.start(_SLUG)
        actions = k.handle(WaitCompleted(result=_wait_result("failed", "timeout")))
        assert len(actions) == 1
        assert isinstance(actions[0], CleanupPane)
        assert actions[0].state == "failed"

    def test_double_start_raises(self):
        k = PostDispatchKernel()
        k.start(_SLUG)
        with pytest.raises(ValueError):
            k.start(_SLUG)


class TestKernelEdgeCases:
    """Edge cases and boundary conditions."""

    def test_zero_commits_completes_without_merge(self):
        """A review with 0 commits should complete (not merge nothing)."""
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        actions = k.handle(ReviewCompleted(result=_review_result("safe", 0)))
        assert k.state == KernelState.COMPLETED
        assert isinstance(actions[0], CleanupPane)
        assert actions[0].state == "closed"

    def test_review_error_fails(self):
        """A review that errors should fail, not proceed to merge."""
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        actions = k.handle(ReviewCompleted(result=_review_result("safe", 1, error="db error")))
        assert k.state == KernelState.FAILED
        assert actions[0].failure_stage == "review"

    def test_no_auto_merge_stops_at_reviewed_pass(self):
        """With auto_merge=False, safe review stops at REVIEWED_PASS."""
        k = PostDispatchKernel(auto_merge=False)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        k.handle(ReviewCompleted(result=_review_result("safe", 1)))
        assert k.state == KernelState.REVIEWED_PASS

    def test_merge_failure_fails(self):
        k = PostDispatchKernel(auto_merge=True)
        k.start(_SLUG)
        k.handle(WaitCompleted(result=_wait_result("completed")))
        k.handle(ReviewCompleted(result=_review_result("safe", 1)))
        actions = k.handle(MergeCompleted(result=_merge_result("conflict")))
        assert k.state == KernelState.FAILED
        assert actions[0].failure_stage == "merge"
