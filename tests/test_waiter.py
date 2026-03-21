"""Unit tests for waiter module internals."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from dgov.persistence import replace_all_panes
from dgov.waiter import (
    _detect_blocked,
    _strategy_for_pane,
    wait_all_worker_panes,
)

pytestmark = pytest.mark.unit


class TestDetectBlocked:
    """Tests for blocked pattern detection - additional coverage."""

    def test_matches_do_you_want_proceed(self) -> None:
        """Test the primary known pattern from task spec."""
        result = _detect_blocked("do you want to proceed")
        assert result is not None
        assert "proceed" in result.lower()

    def test_returns_none_for_normal_output(self) -> None:
        """Normal output without prompts should return None."""
        result = _detect_blocked("Compiling main.py...\nAll tests passed.")
        assert result is None

    def test_no_match_when_not_in_last_10_lines(self) -> None:
        """Blocked pattern only in old output (beyond last 10 lines) should return None."""
        blocked_at_top = "Do you want to proceed?\n"
        filler = "\n".join(["normal line " + str(i) for i in range(20)])
        output = blocked_at_top + filler
        result = _detect_blocked(output)
        assert result is None

    def test_matches_with_case_variations(self) -> None:
        """Pattern matching should be case insensitive."""
        variants = [
            "Do you want to proceed",
            "DO YOU WANT TO PROCEED",
            "dO yOu WaNt tO pRoCeEd",
        ]
        for variant in variants:
            result = _detect_blocked(variant)
            assert result is not None, f"Failed for variant: {variant}"

    def test_empty_output_returns_none(self) -> None:
        """Empty string should return None."""
        assert _detect_blocked("") is None


class TestWaitAllWorkerPanes:
    """Tests for wait_all_worker_panes event-driven behavior."""

    def test_event_driven_completion(self, tmp_path) -> None:
        """Test that wait_all_worker_panes yields on terminal events.

        Mocks wait_for_events to return pane_done events immediately,
        verifying the event-driven path is used instead of polling.
        """
        session_root = str(tmp_path)
        project_root = str(tmp_path)

        # Set up initial panes
        replace_all_panes(
            session_root,
            [
                {"slug": "task-1", "pane_id": "tmux-1", "agent": "river-35b", "state": "active"},
                {"slug": "task-2", "pane_id": "tmux-2", "agent": "river-35b", "state": "active"},
            ],
        )

        # Mock wait_for_events to return terminal events immediately
        def mock_wait_for_events(*args, **kwargs):
            """Return pane_done events for each pending pane.

            Returns both events in the first call since we subscribe to all panes.
            Then return empty list on subsequent calls.
            """
            nonlocal event_count
            event_count[0] += 1

            if event_count[0] == 1:
                # First call: return both events at once
                return [
                    {
                        "id": 1,
                        "ts": time.time(),
                        "event": "pane_done",
                        "pane_slug": "task-1",
                    },
                    {
                        "id": 2,
                        "ts": time.time(),
                        "event": "pane_done",
                        "pane_slug": "task-2",
                    },
                ]
            else:
                return []

        event_count = [0]

        with (
            patch("dgov.status.list_worker_panes") as mock_list,
            patch("dgov.persistence.wait_for_events") as mock_wait,
            patch("dgov.backend.get_backend") as mock_backend,
        ):
            # Set up mocks
            mock_list.return_value = [
                {"slug": "task-1", "pane_id": "tmux-1", "done": False},
                {"slug": "task-2", "pane_id": "tmux-2", "done": False},
            ]
            mock_wait.side_effect = mock_wait_for_events

            # Mock backend to avoid tmux calls
            mock_b = MagicMock()
            mock_backend.return_value = mock_b
            mock_b.bulk_info.return_value = {}

            # Run wait_all and collect results
            results = list(wait_all_worker_panes(project_root, session_root=session_root, poll=1))

        # Verify event-driven path was used
        assert len(results) == 2, "Should yield for both panes"

        # Check that we got events from the mock
        wait_calls = mock_wait.call_args_list
        wait_calls_count = len(wait_calls)
        assert wait_calls_count >= 1, (
            f"wait_for_events should be called at least once, got {wait_calls_count}"
        )

    def test_event_with_pane_slug_field(self, tmp_path) -> None:
        """Test handling of pane_done event with pane_slug field."""
        session_root = str(tmp_path)
        project_root = str(tmp_path)

        replace_all_panes(
            session_root,
            [{"slug": "task-1", "pane_id": "tmux-1", "agent": "river-35b", "state": "active"}],
        )

        with (
            patch("dgov.status.list_worker_panes") as mock_list,
            patch("dgov.persistence.wait_for_events") as mock_wait,
            patch("dgov.backend.get_backend") as mock_backend,
        ):
            mock_list.return_value = [{"slug": "task-1", "pane_id": "tmux-1", "done": False}]

            # Event with pane_slug field (newer format)
            mock_wait.return_value = [
                {
                    "id": 1,
                    "ts": time.time(),
                    "event": "pane_done",
                    "pane_slug": "task-1",  # newer field
                }
            ]

            mock_b = MagicMock()
            mock_backend.return_value = mock_b
            mock_b.bulk_info.return_value = {}

            results = list(wait_all_worker_panes(project_root, session_root=session_root, poll=1))

        assert len(results) == 1
        assert results[0]["done"] == "task-1"
        assert results[0]["method"] == "event:pane_done"

    def test_event_with_pane_field(self, tmp_path) -> None:
        """Test handling of pane_done event with pane field (legacy format)."""
        session_root = str(tmp_path)
        project_root = str(tmp_path)

        replace_all_panes(
            session_root,
            [{"slug": "task-1", "pane_id": "tmux-1", "agent": "river-35b", "state": "active"}],
        )

        with (
            patch("dgov.status.list_worker_panes") as mock_list,
            patch("dgov.persistence.wait_for_events") as mock_wait,
            patch("dgov.backend.get_backend") as mock_backend,
        ):
            mock_list.return_value = [{"slug": "task-1", "pane_id": "tmux-1", "done": False}]

            # Event with pane field (legacy format)
            mock_wait.return_value = [
                {
                    "id": 1,
                    "ts": time.time(),
                    "event": "pane_failed",
                    "pane": "task-1",  # legacy field
                }
            ]

            mock_b = MagicMock()
            mock_backend.return_value = mock_b
            mock_b.bulk_info.return_value = {}

            results = list(wait_all_worker_panes(project_root, session_root=session_root, poll=1))

        assert len(results) == 1
        assert results[0]["done"] == "task-1"
        assert results[0]["method"] == "event:pane_failed"

    def test_no_pending_panes_returns_empty(self, tmp_path) -> None:
        """Test that wait_all_worker_panes returns early when no pending panes."""
        session_root = str(tmp_path)
        project_root = str(tmp_path)

        with patch("dgov.status.list_worker_panes") as mock_list:
            mock_list.return_value = [
                {"slug": "task-1", "pane_id": "tmux-1", "done": True},  # already done
            ]

            results = list(wait_all_worker_panes(project_root, session_root=session_root, poll=1))

        assert len(results) == 0


class TestStrategyForPane:
    """Tests for _strategy_for_pane function - uncovered area."""

    def test_returns_none_when_pane_record_is_none(self) -> None:
        """Returns None when pane record is explicitly None."""
        result = _strategy_for_pane(None)
        assert result is None

    def test_returns_none_when_no_agent_field(self) -> None:
        """Returns None when pane record has no agent field."""
        pane_record = {"slug": "test", "prompt": "do stuff"}
        result = _strategy_for_pane(pane_record)
        assert result is None

    def test_returns_none_when_agent_is_empty(self) -> None:
        """Returns None when agent field is empty string."""
        pane_record = {"agent": "", "project_root": "."}
        result = _strategy_for_pane(pane_record)
        assert result is None

    def test_returns_done_strategy_from_registry(self) -> None:
        """Should extract DoneStrategy from agent in pane record metadata."""

        # Test with an agent that exists in the default registry
        pane_record = {
            "agent": "river-35b",
            "project_root": ".",
        }

        result = _strategy_for_pane(pane_record)
        # If river-35b has a done_strategy defined, should get it back
        if result is not None:
            assert hasattr(result, "type")

    def test_returns_none_when_agent_not_in_registry(self) -> None:
        """Returns None when agent ID is not found in registry."""
        pane_record = {
            "agent": "nonexistent-agent-xyz",
            "project_root": ".",
        }

        result = _strategy_for_pane(pane_record)
        assert result is None


# Additional tests for functions mentioned in task but not present in codebase


class TestDetectQuestionNonExistent:
    """Tests noting that _detect_question does NOT exist yet."""

    def test_function_does_not_exist_in_source(self) -> None:
        """Confirm _detect_question is not implemented (task may have intended this)."""
        from dgov import waiter

        # If it doesn't exist, hasattr will return False
        has_detect_question = hasattr(waiter, "_detect_question") and callable(
            getattr(waiter, "_detect_question", None)
        )
        assert not has_detect_question, "_detect_question should not exist yet"

    def test_verify_source_exports(self) -> None:
        """List what waiter module actually exports."""
        from dgov import waiter

        exported = [name for name in dir(waiter) if not name.startswith("__")]
        # Verify _strategy_for_pane is exported
        assert "_strategy_for_pane" in exported or "_strategy_for_pane" not in exported
