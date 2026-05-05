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

    def test_filter_by_task_slug(self, tmp_path):
        """Filter by task_slug returns only matching events."""
        session_root = _session(tmp_path)

        emit_event(session_root, event="worker_log", pane="p1", task_slug="task-a")
        emit_event(session_root, event="worker_log", pane="p2", task_slug="task-b")
        emit_event(session_root, event="worker_log", pane="p3", task_slug="task-a")

        events = read_events(session_root, task_slug="task-a")
        assert len(events) == 2
        assert {ev["pane"] for ev in events} == {"p1", "p3"}

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

    def test_valid_events_contains_run_completed(self):
        """VALID_EVENTS should contain run_completed event."""
        assert "run_completed" in VALID_EVENTS

    def test_valid_events_contains_semantic_settlement_events(self):
        """VALID_EVENTS contains the semantic settlement event family."""
        semantic_events = {
            "integration_risk_scored",
            "integration_overlap_detected",
            "integration_candidate_passed",
            "integration_candidate_failed",
            "semantic_gate_rejected",
        }
        for event in semantic_events:
            assert event in VALID_EVENTS, f"{event} should be in VALID_EVENTS"


class TestSemanticSettlementEvents:
    """Tests for semantic settlement event round-tripping through persistence."""

    def test_emit_integration_risk_scored_roundtrip(self, tmp_path):
        """integration_risk_scored event emits and reads back correctly."""
        from dgov.semantic_settlement import (
            IntegrationRiskRecord,
            RiskLevel,
            emit_integration_risk_scored,
        )

        session_root = _session(tmp_path)

        record = IntegrationRiskRecord(
            task_slug="task-123",
            target_head_sha="abc123",
            task_base_sha="def456",
            task_commit_sha="ghi789",
            risk_level=RiskLevel.HIGH,
            claimed_files=("src/a.py", "src/b.py"),
            changed_files=("src/a.py",),
            python_overlap_detected=True,
        )

        emit_integration_risk_scored(
            emit_event,
            session_root,
            "test-plan",
            record,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "integration_risk_scored"
        assert ev["task_slug"] == "task-123"
        assert ev["risk_level"] == "high"
        assert ev["target_head_sha"] == "abc123"
        assert ev["task_commit_sha"] == "ghi789"
        assert ev["python_overlap_detected"] is True

    def test_emit_integration_overlap_detected_roundtrip(self, tmp_path):
        """integration_overlap_detected event emits and reads back correctly."""
        from dgov.semantic_settlement import SymbolOverlap, emit_integration_overlap_detected

        session_root = _session(tmp_path)

        evidence = SymbolOverlap(
            symbol_name="process_data",
            symbol_type="function",
            file_path="src/runner.py",
            task_line_range=(10, 20),
        )

        emit_integration_overlap_detected(
            emit_event,
            session_root,
            "test-plan",
            "task-123",
            evidence,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "integration_overlap_detected"
        assert ev["task_slug"] == "task-123"
        assert ev["evidence"]["_kind"] == "SymbolOverlap"
        assert ev["evidence"]["symbol_name"] == "process_data"

    def test_emit_integration_candidate_passed_roundtrip(self, tmp_path):
        """integration_candidate_passed event emits and reads back correctly."""
        from dgov.semantic_settlement import (
            IntegrationCandidateVerdict,
            emit_integration_candidate_passed,
        )

        session_root = _session(tmp_path)

        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=True,
        )

        emit_integration_candidate_passed(
            emit_event,
            session_root,
            "test-plan",
            verdict,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "integration_candidate_passed"
        assert ev["task_slug"] == "task-123"
        assert ev["candidate_sha"] == "candidate-abc"
        assert ev["passed"] is True

    def test_emit_integration_candidate_failed_roundtrip(self, tmp_path):
        """integration_candidate_failed event emits and reads back correctly."""
        from dgov.semantic_settlement import (
            FailureClass,
            IntegrationCandidateVerdict,
            TextConflict,
            emit_integration_candidate_failed,
        )

        session_root = _session(tmp_path)

        evidence = TextConflict(
            file_path="src/conflict.py",
            conflict_markers=3,
        )
        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=False,
            failure_class=FailureClass.TEXT_CONFLICT,
            evidence=(evidence,),
            error_message="Merge conflict detected",
        )

        emit_integration_candidate_failed(
            emit_event,
            session_root,
            "test-plan",
            verdict,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "integration_candidate_failed"
        assert ev["passed"] is False
        assert ev["failure_class"] == "text_conflict"
        assert ev["error_message"] == "Merge conflict detected"
        assert ev["evidence"][0]["_kind"] == "TextConflict"

    def test_emit_semantic_gate_rejected_roundtrip(self, tmp_path):
        """semantic_gate_rejected event emits and reads back correctly."""
        from dgov.semantic_settlement import (
            FailureClass,
            SemanticGateVerdict,
            SymbolOverlap,
            emit_semantic_gate_rejected,
        )

        session_root = _session(tmp_path)

        evidence = SymbolOverlap(
            symbol_name="process_data",
            symbol_type="function",
            file_path="src/worker.py",
        )
        verdict = SemanticGateVerdict(
            task_slug="task-123",
            gate_name="same_symbol_edit",
            passed=False,
            failure_class=FailureClass.SAME_SYMBOL_EDIT,
            evidence=(evidence,),
            error_message="Both sides modified process_data",
        )

        emit_semantic_gate_rejected(
            emit_event,
            session_root,
            "test-plan",
            verdict,
        )

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "semantic_gate_rejected"
        assert ev["gate_name"] == "same_symbol_edit"
        assert ev["failure_class"] == "same_symbol_edit"

    def test_semantic_settlement_events_filter_by_plan_name(self, tmp_path):
        """Semantic settlement events can be filtered by plan_name."""
        from dgov.semantic_settlement import (
            IntegrationCandidateVerdict,
            IntegrationRiskRecord,
            RiskLevel,
            emit_integration_candidate_passed,
            emit_integration_risk_scored,
        )

        session_root = _session(tmp_path)

        # Emit for plan-a
        record_a = IntegrationRiskRecord(
            task_slug="task-a",
            target_head_sha="abc",
            task_base_sha="def",
            task_commit_sha="ghi",
            risk_level=RiskLevel.LOW,
            claimed_files=(),
            changed_files=(),
        )
        emit_integration_risk_scored(emit_event, session_root, "plan-a", record_a)

        # Emit for plan-b
        record_b = IntegrationRiskRecord(
            task_slug="task-b",
            target_head_sha="abc",
            task_base_sha="def",
            task_commit_sha="ghi",
            risk_level=RiskLevel.HIGH,
            claimed_files=(),
            changed_files=(),
        )
        emit_integration_risk_scored(emit_event, session_root, "plan-b", record_b)

        verdict_b = IntegrationCandidateVerdict(
            task_slug="task-b",
            candidate_sha="candidate",
            target_head_sha="target",
            passed=True,
        )
        emit_integration_candidate_passed(emit_event, session_root, "plan-b", verdict_b)

        # Filter by plan
        plan_a_events = read_events(session_root, plan_name="plan-a")
        plan_b_events = read_events(session_root, plan_name="plan-b")

        assert len(plan_a_events) == 1
        assert plan_a_events[0]["plan_name"] == "plan-a"
        assert plan_a_events[0]["risk_level"] == "low"

        assert len(plan_b_events) == 2

    def test_semantic_settlement_events_filter_by_task_slug(self, tmp_path):
        """Semantic settlement events can be filtered by task_slug."""
        from dgov.semantic_settlement import (
            IntegrationCandidateVerdict,
            emit_integration_candidate_passed,
        )

        session_root = _session(tmp_path)

        # Emit for different tasks
        for slug in ["task-1", "task-2", "task-1"]:
            verdict = IntegrationCandidateVerdict(
                task_slug=slug,
                candidate_sha=f"candidate-{slug}",
                target_head_sha="target",
                passed=True,
            )
            emit_integration_candidate_passed(emit_event, session_root, "plan", verdict)

        # Filter by task
        task_1_events = read_events(session_root, task_slug="task-1")
        task_2_events = read_events(session_root, task_slug="task-2")

        assert len(task_1_events) == 2
        assert len(task_2_events) == 1
        assert task_2_events[0]["candidate_sha"] == "candidate-task-2"


class TestSettlementPhaseEvents:
    """Tests for settlement phase telemetry events."""

    def test_valid_events_contains_settlement_phase_events(self):
        """VALID_EVENTS should contain settlement phase event types."""
        assert "settlement_phase_started" in VALID_EVENTS
        assert "settlement_phase_completed" in VALID_EVENTS

    def test_emit_settlement_phase_started_roundtrip(self, tmp_path):
        """settlement_phase_started event emits and reads back correctly."""
        from dgov.event_types import SettlementPhaseStarted, serialize_event

        session_root = _session(tmp_path)

        event = SettlementPhaseStarted(
            pane="test-pane",
            plan_name="test-plan",
            task_slug="task-123",
            phase="risk_assessment",
        )

        event_name, pane, kwargs = serialize_event(event)
        emit_event(session_root, event=event_name, pane=pane, **kwargs)

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "settlement_phase_started"
        assert ev["pane"] == "test-pane"
        assert ev["plan_name"] == "test-plan"
        assert ev["task_slug"] == "task-123"
        assert ev["phase"] == "risk_assessment"

    def test_emit_settlement_phase_completed_success_roundtrip(self, tmp_path):
        """settlement_phase_completed success event emits and reads back correctly."""
        from dgov.event_types import SettlementPhaseCompleted, serialize_event

        session_root = _session(tmp_path)

        event = SettlementPhaseCompleted(
            pane="test-pane",
            plan_name="test-plan",
            task_slug="task-123",
            phase="risk_assessment",
            status="success",
            duration_s=1.5,
            error=None,
        )

        event_name, pane, kwargs = serialize_event(event)
        emit_event(session_root, event=event_name, pane=pane, **kwargs)

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "settlement_phase_completed"
        assert ev["pane"] == "test-pane"
        assert ev["plan_name"] == "test-plan"
        assert ev["task_slug"] == "task-123"
        assert ev["phase"] == "risk_assessment"
        assert ev["status"] == "success"
        assert ev["duration_s"] == 1.5
        assert "error" not in ev

    def test_emit_settlement_phase_completed_failure_roundtrip(self, tmp_path):
        """settlement_phase_completed failure event emits and reads back correctly."""
        from dgov.event_types import SettlementPhaseCompleted, serialize_event

        session_root = _session(tmp_path)

        event = SettlementPhaseCompleted(
            pane="test-pane",
            plan_name="test-plan",
            task_slug="task-456",
            phase="semantic_gate",
            status="failed",
            duration_s=0.8,
            error="Gate rejected: syntax conflict detected",
        )

        event_name, pane, kwargs = serialize_event(event)
        emit_event(session_root, event=event_name, pane=pane, **kwargs)

        events = read_events(session_root)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "settlement_phase_completed"
        assert ev["pane"] == "test-pane"
        assert ev["plan_name"] == "test-plan"
        assert ev["task_slug"] == "task-456"
        assert ev["phase"] == "semantic_gate"
        assert ev["status"] == "failed"
        assert ev["duration_s"] == 0.8
        assert ev["error"] == "Gate rejected: syntax conflict detected"

    def test_settlement_phase_events_filter_by_plan_name(self, tmp_path):
        """Settlement phase events can be filtered by plan_name."""
        from dgov.event_types import SettlementPhaseStarted, serialize_event

        session_root = _session(tmp_path)

        for plan in ["plan-a", "plan-b", "plan-a"]:
            event = SettlementPhaseStarted(
                pane="pane-1",
                plan_name=plan,
                task_slug="task-1",
                phase="risk_assessment",
            )
            event_name, pane, kwargs = serialize_event(event)
            emit_event(session_root, event=event_name, pane=pane, **kwargs)

        plan_a_events = read_events(session_root, plan_name="plan-a")
        plan_b_events = read_events(session_root, plan_name="plan-b")

        assert len(plan_a_events) == 2
        assert len(plan_b_events) == 1
        assert plan_b_events[0]["plan_name"] == "plan-b"

    def test_settlement_phase_events_filter_by_task_slug(self, tmp_path):
        """Settlement phase events can be filtered by task_slug."""
        from dgov.event_types import SettlementPhaseCompleted, serialize_event

        session_root = _session(tmp_path)

        for slug in ["task-x", "task-y", "task-x"]:
            event = SettlementPhaseCompleted(
                pane="pane-1",
                plan_name="plan",
                task_slug=slug,
                phase="integration",
                status="success",
                duration_s=1.0,
            )
            event_name, pane, kwargs = serialize_event(event)
            emit_event(session_root, event=event_name, pane=pane, **kwargs)

        task_x_events = read_events(session_root, task_slug="task-x")
        task_y_events = read_events(session_root, task_slug="task-y")

        assert len(task_x_events) == 2
        assert len(task_y_events) == 1

    def test_settlement_phase_events_typed_deserialization(self, tmp_path):
        """Settlement phase events deserialize correctly via deserialize_event."""
        from dgov.event_types import (
            SettlementPhaseCompleted,
            SettlementPhaseStarted,
            deserialize_event,
            serialize_event,
        )

        session_root = _session(tmp_path)

        # Emit started event
        started = SettlementPhaseStarted(
            pane="test-pane",
            plan_name="test-plan",
            task_slug="task-789",
            phase="candidate_validation",
        )
        event_name, pane, kwargs = serialize_event(started)
        emit_event(session_root, event=event_name, pane=pane, **kwargs)

        # Emit completed event
        completed = SettlementPhaseCompleted(
            pane="test-pane",
            plan_name="test-plan",
            task_slug="task-789",
            phase="candidate_validation",
            status="success",
            duration_s=2.5,
            error=None,
        )
        event_name, pane, kwargs = serialize_event(completed)
        emit_event(session_root, event=event_name, pane=pane, **kwargs)

        # Read back and deserialize
        events = read_events(session_root)
        assert len(events) == 2

        # First event should deserialize to SettlementPhaseStarted
        deserialized_start = deserialize_event(events[0])
        assert isinstance(deserialized_start, SettlementPhaseStarted)
        assert deserialized_start.pane == "test-pane"
        assert deserialized_start.plan_name == "test-plan"
        assert deserialized_start.task_slug == "task-789"
        assert deserialized_start.phase == "candidate_validation"

        # Second event should deserialize to SettlementPhaseCompleted
        deserialized_complete = deserialize_event(events[1])
        assert isinstance(deserialized_complete, SettlementPhaseCompleted)
        assert deserialized_complete.pane == "test-pane"
        assert deserialized_complete.plan_name == "test-plan"
        assert deserialized_complete.task_slug == "task-789"
        assert deserialized_complete.phase == "candidate_validation"
        assert deserialized_complete.status == "success"
        assert deserialized_complete.duration_s == 2.5
        assert deserialized_complete.error is None
