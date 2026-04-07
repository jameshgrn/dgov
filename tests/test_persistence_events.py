"""Tests for persistence events — emit_event, read_events, latest_event_id, reset_plan_state."""

from __future__ import annotations

import pytest

from dgov.persistence.events import (
    emit_event,
    latest_event_id,
    read_events,
    reset_plan_state,
)
from dgov.persistence.schema import VALID_EVENTS

pytestmark = pytest.mark.unit


def _session(tmp_path):
    """Create the .dgov directory and return the session_root string."""
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    return str(tmp_path)


class TestEmitEvent:
    """Tests for emit_event function."""

    def test_emit_valid_event_and_read_back(self, tmp_path):
        """Emit a valid event and read it back to verify fields."""
        session_root = _session(tmp_path)

        emit_event(
            session_root,
            event="dag_task_dispatched",
            pane="p1",
            plan_name="plan",
            task_slug="slug-a",
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "dag_task_dispatched"
        assert ev["pane"] == "p1"
        assert ev["plan_name"] == "plan"
        assert ev["task_slug"] == "slug-a"
        assert "id" in ev
        assert "ts" in ev

    def test_emit_invalid_event_raises_valueerror(self, tmp_path):
        """Emit with an invalid event name raises ValueError."""
        session_root = _session(tmp_path)

        with pytest.raises(ValueError, match="Unknown event: 'invalid_event'"):
            emit_event(session_root, event="invalid_event", pane="p1")

        # Verify no events were written
        assert read_events(session_root) == []

    def test_emit_extra_kwargs_stored_in_data_blob(self, tmp_path):
        """Emit with extra kwargs not in typed columns stores them in data JSON blob."""
        session_root = _session(tmp_path)

        emit_event(
            session_root,
            event="worker_log",
            pane="p1",
            custom_field="custom_value",
            another_extra=123,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "worker_log"
        assert ev["pane"] == "p1"
        assert ev["custom_field"] == "custom_value"
        assert ev["another_extra"] == 123


class TestReadEventsFiltering:
    """Tests for read_events filtering capabilities."""

    def test_filter_by_slug(self, tmp_path):
        """Filter by pane/slug returns only matching events."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan")

        events = read_events(session_root, slug="p2")
        assert len(events) == 1
        assert events[0]["pane"] == "p2"

    def test_filter_by_plan_name(self, tmp_path):
        """Filter by plan_name returns only matching events."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-a")
        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-b")
        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-c")

        events = read_events(session_root, plan_name="plan-b")
        assert len(events) == 1
        assert events[0]["plan_name"] == "plan-b"

    def test_filter_by_after_id(self, tmp_path):
        """Use after_id to get only new events since a known position."""
        session_root = _session(tmp_path)

        # Emit first 2 events
        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan")
        last_id = latest_event_id(session_root)

        # Emit 2 more events
        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p4", plan_name="plan")

        # Read with after_id should return only the 2 new ones
        events = read_events(session_root, after_id=last_id)
        assert len(events) == 2
        assert events[0]["pane"] == "p3"
        assert events[1]["pane"] == "p4"

    def test_limit_parameter_returns_at_most_n_events(self, tmp_path):
        """Use limit parameter to return at most N most recent events in chronological order."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p4", plan_name="plan")
        emit_event(session_root, event="worker_log", pane="p5", plan_name="plan")

        events = read_events(session_root, limit=3)
        assert len(events) == 3
        # Limit returns the N most recent events in chronological order
        # So with 5 events total and limit=3, we get p3, p4, p5 (the 3 newest)
        assert events[0]["pane"] == "p3"
        assert events[1]["pane"] == "p4"
        assert events[2]["pane"] == "p5"

    def test_limit_with_filter(self, tmp_path):
        """Limit combined with filter returns N most recent matching events."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-a")
        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan-b")
        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan-a")
        emit_event(session_root, event="worker_log", pane="p4", plan_name="plan-a")

        events = read_events(session_root, plan_name="plan-a", limit=2)
        assert len(events) == 2
        # Returns 2 most recent plan-a events in chronological order: p3, p4
        assert events[0]["pane"] == "p3"
        assert events[1]["pane"] == "p4"


class TestLatestEventId:
    """Tests for latest_event_id function."""

    def test_empty_db_returns_zero(self, tmp_path):
        """Empty database returns 0."""
        session_root = _session(tmp_path)

        assert latest_event_id(session_root) == 0

    def test_after_emitting_events_returns_highest_id(self, tmp_path):
        """After emitting events, returns highest id."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan")
        assert latest_event_id(session_root) == 1

        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan")
        assert latest_event_id(session_root) == 2

        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan")
        assert latest_event_id(session_root) == 3


class TestResetPlanState:
    """Tests for reset_plan_state function."""

    def test_reset_one_plan_keeps_other_plan_events(self, tmp_path):
        """Emit events for 2 plans, reset one, verify only other plan's events remain."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-a")
        emit_event(session_root, event="worker_log", pane="p2", plan_name="plan-b")
        emit_event(session_root, event="worker_log", pane="p3", plan_name="plan-a")
        emit_event(session_root, event="worker_log", pane="p4", plan_name="plan-b")

        # Reset plan-a
        reset_plan_state(session_root, plan_name="plan-a")

        # Only plan-b events should remain
        events = read_events(session_root)
        assert len(events) == 2
        assert all(ev["plan_name"] == "plan-b" for ev in events)
        assert {ev["pane"] for ev in events} == {"p2", "p4"}

    def test_reset_nonexistent_plan_noop(self, tmp_path):
        """Reset a plan with no events is a no-op."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", plan_name="plan-a")

        reset_plan_state(session_root, plan_name="nonexistent")

        # Original event should still exist
        events = read_events(session_root)
        assert len(events) == 1
        assert events[0]["plan_name"] == "plan-a"


class TestValidEvents:
    """Sanity check that VALID_EVENTS is importable and contains expected events."""

    def test_valid_events_contains_expected(self):
        """VALID_EVENTS should contain the events used in tests."""
        assert "dag_task_dispatched" in VALID_EVENTS
        assert "worker_log" in VALID_EVENTS
        assert "invalid_event" not in VALID_EVENTS
