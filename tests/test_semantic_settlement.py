"""Tests for semantic_settlement module — contract types and payload shaping."""

from __future__ import annotations

from typing import Any

import pytest

from dgov.semantic_settlement import (
    DuplicateDefinition,
    FailureClass,
    IntegrationCandidateVerdict,
    IntegrationRiskRecord,
    RiskLevel,
    SemanticGateVerdict,
    SignatureDrift,
    SymbolOverlap,
    SyntaxConflict,
    TextConflict,
    _deserialize_evidence,
    _serialize_evidence,
    emit_integration_candidate_failed,
    emit_integration_candidate_passed,
    emit_integration_overlap_detected,
    emit_integration_risk_scored,
    emit_semantic_gate_rejected,
    parse_integration_candidate_verdict,
    parse_integration_risk_record,
    parse_semantic_gate_verdict,
)

pytestmark = pytest.mark.unit


class MockEmit:
    """Callable mock that captures emit calls for verification."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session_root: str, event: str, pane: str, **kwargs: Any) -> None:
        self.calls.append({
            "session_root": session_root,
            "event": event,
            "pane": pane,
            "kwargs": kwargs,
        })


@pytest.fixture
def mock_emit():
    """Create a mock emit function that captures calls."""
    return MockEmit()


class TestFailureClassEnum:
    """Tests for FailureClass StrEnum."""

    def test_all_failure_classes_defined(self):
        """All taxonomy classes from DESIGN.md are present."""
        assert set(FailureClass) == {
            FailureClass.TEXT_CONFLICT,
            FailureClass.SYNTAX_CONFLICT,
            FailureClass.SAME_SYMBOL_EDIT,
            FailureClass.DUPLICATE_DEFINITION,
            FailureClass.SIGNATURE_DRIFT,
            FailureClass.ORDERING_CONFLICT,
            FailureClass.BEHAVIORAL_MISMATCH,
        }

    def test_failure_class_values(self):
        """FailureClass values match design specification."""
        assert FailureClass.TEXT_CONFLICT == "text_conflict"
        assert FailureClass.SYNTAX_CONFLICT == "syntax_conflict"
        assert FailureClass.SAME_SYMBOL_EDIT == "same_symbol_edit"
        assert FailureClass.DUPLICATE_DEFINITION == "duplicate_definition"
        assert FailureClass.SIGNATURE_DRIFT == "signature_drift"
        assert FailureClass.ORDERING_CONFLICT == "ordering_conflict"
        assert FailureClass.BEHAVIORAL_MISMATCH == "behavioral_mismatch"

    def test_failure_class_from_string(self):
        """FailureClass can be constructed from string values."""
        assert FailureClass("text_conflict") == FailureClass.TEXT_CONFLICT
        assert FailureClass("syntax_conflict") == FailureClass.SYNTAX_CONFLICT


class TestRiskLevelEnum:
    """Tests for RiskLevel StrEnum."""

    def test_all_risk_levels_defined(self):
        """All risk levels from design are present."""
        assert set(RiskLevel) == {
            RiskLevel.NONE,
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        }

    def test_risk_level_values(self):
        """RiskLevel values are lowercase strings."""
        assert RiskLevel.NONE == "none"
        assert RiskLevel.LOW == "low"
        assert RiskLevel.MEDIUM == "medium"
        assert RiskLevel.HIGH == "high"
        assert RiskLevel.CRITICAL == "critical"

    def test_risk_level_ordering_comparison(self):
        """Risk levels can be compared by severity for logic checks."""
        severity_order = [
            RiskLevel.NONE,
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ]
        for i, level in enumerate(severity_order):
            assert level == severity_order[i]


class TestEvidenceDataclasses:
    """Tests for evidence dataclass construction."""

    def test_symbol_overlap_construction(self):
        """SymbolOverlap can be constructed with all fields."""
        overlap = SymbolOverlap(
            symbol_name="process",
            symbol_type="function",
            file_path="src/runner.py",
            task_line_range=(10, 20),
            target_line_range=(10, 25),
        )
        assert overlap.symbol_name == "process"
        assert overlap.task_line_range == (10, 20)

    def test_duplicate_definition_construction(self):
        """DuplicateDefinition captures multiple file paths and line numbers."""
        dup = DuplicateDefinition(
            symbol_name="config",
            symbol_type="variable",
            file_paths=("src/a.py", "src/b.py"),
            line_numbers=((10, 15), (20, 25)),
        )
        assert dup.symbol_name == "config"
        assert dup.line_numbers == ((10, 15), (20, 25))

    def test_signature_drift_construction(self):
        """SignatureDrift captures signature changes."""
        drift = SignatureDrift(
            symbol_name="run_task",
            file_path="src/worker.py",
            base_signature="def run_task() -> None",
            integrated_signature="def run_task(timeout: int) -> None",
        )
        assert drift.symbol_name == "run_task"
        assert "timeout: int" in drift.integrated_signature

    def test_syntax_conflict_construction(self):
        """SyntaxConflict captures parse errors with optional fields."""
        conflict = SyntaxConflict(
            file_path="src/bad.py",
            line_number=42,
            column=15,
            error_message="invalid syntax",
            parser_used="python",
        )
        assert conflict.file_path == "src/bad.py"
        assert conflict.line_number == 42
        assert conflict.parser_used == "python"

    def test_text_conflict_construction(self):
        """TextConflict captures Git merge conflicts."""
        conflict = TextConflict(
            file_path="src/conflict.py",
            conflict_markers=3,
            base_lines=(40, 50),
            ours_lines=(45, 55),
            theirs_lines=(40, 48),
        )
        assert conflict.file_path == "src/conflict.py"
        assert conflict.conflict_markers == 3
        assert conflict.base_lines == (40, 50)


class TestVerdictDataclasses:
    """Tests for verdict dataclass construction."""

    def test_integration_risk_record_construction(self):
        """IntegrationRiskRecord captures risk assessment with evidence."""
        evidence = SymbolOverlap(
            symbol_name="process",
            symbol_type="function",
            file_path="src/runner.py",
        )
        record = IntegrationRiskRecord(
            task_slug="task-123",
            target_head_sha="abc123",
            task_base_sha="def456",
            task_commit_sha="ghi789",
            risk_level=RiskLevel.HIGH,
            claimed_files=("src/runner.py",),
            changed_files=("src/runner.py",),
            python_overlap_detected=True,
            overlap_evidence=(evidence,),
            computed_at=0.0,
        )
        assert record.task_slug == "task-123"
        assert record.risk_level == RiskLevel.HIGH
        assert len(record.overlap_evidence) == 1

    def test_integration_candidate_verdict_passed(self):
        """Passed candidate verdict has no failure class."""
        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=True,
        )
        assert verdict.passed is True
        assert verdict.failure_class is None

    def test_integration_candidate_verdict_failed(self):
        """Failed candidate verdict includes failure class and evidence."""
        evidence = TextConflict(file_path="src/conflict.py", conflict_markers=2)
        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=False,
            failure_class=FailureClass.TEXT_CONFLICT,
            evidence=(evidence,),
            error_message="Merge conflict detected",
        )
        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.TEXT_CONFLICT
        assert len(verdict.evidence) == 1

    def test_semantic_gate_verdict_passed(self):
        """Passed gate has no failure."""
        verdict = SemanticGateVerdict(
            task_slug="task-123",
            gate_name="same_symbol_edit",
            passed=True,
        )
        assert verdict.passed is True
        assert verdict.failure_class is None

    def test_semantic_gate_verdict_failed(self):
        """Failed gate includes precise reason with evidence."""
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
        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SAME_SYMBOL_EDIT


class TestEvidenceSerialization:
    """Tests for evidence serialization/deserialization round-trip."""

    def test_symbol_overlap_roundtrip(self):
        """SymbolOverlap serializes and deserializes correctly."""
        original = SymbolOverlap(
            symbol_name="process",
            symbol_type="function",
            file_path="src/runner.py",
            task_line_range=(10, 20),
            target_line_range=(10, 25),
        )
        serialized = _serialize_evidence(original)
        deserialized = _deserialize_evidence(serialized)

        assert isinstance(deserialized, SymbolOverlap)
        assert deserialized.symbol_name == original.symbol_name
        assert deserialized.symbol_type == original.symbol_type
        assert deserialized.file_path == original.file_path
        assert deserialized.task_line_range == original.task_line_range
        assert deserialized.target_line_range == original.target_line_range

    def test_duplicate_definition_roundtrip(self):
        """DuplicateDefinition serializes and deserializes correctly."""
        original = DuplicateDefinition(
            symbol_name="config",
            symbol_type="variable",
            file_paths=("src/a.py", "src/b.py"),
            line_numbers=((10, 15), (20, 25)),
        )
        serialized = _serialize_evidence(original)
        deserialized = _deserialize_evidence(serialized)

        assert isinstance(deserialized, DuplicateDefinition)
        assert deserialized.symbol_name == original.symbol_name
        assert deserialized.file_paths == original.file_paths
        assert deserialized.line_numbers == original.line_numbers

    def test_signature_drift_roundtrip(self):
        """SignatureDrift serializes and deserializes correctly."""
        original = SignatureDrift(
            symbol_name="run_task",
            file_path="src/worker.py",
            base_signature="def run_task() -> None",
            integrated_signature="def run_task(timeout: int) -> None",
        )
        serialized = _serialize_evidence(original)
        deserialized = _deserialize_evidence(serialized)

        assert isinstance(deserialized, SignatureDrift)
        assert deserialized.symbol_name == original.symbol_name
        assert deserialized.base_signature == original.base_signature
        assert deserialized.integrated_signature == original.integrated_signature

    def test_syntax_conflict_roundtrip(self):
        """SyntaxConflict serializes and deserializes correctly."""
        original = SyntaxConflict(
            file_path="src/bad.py",
            line_number=42,
            column=10,
            error_message="invalid syntax",
            parser_used="python",
        )
        serialized = _serialize_evidence(original)
        deserialized = _deserialize_evidence(serialized)

        assert isinstance(deserialized, SyntaxConflict)
        assert deserialized.file_path == original.file_path
        assert deserialized.line_number == original.line_number
        assert deserialized.error_message == original.error_message

    def test_text_conflict_roundtrip(self):
        """TextConflict serializes and deserializes correctly."""
        original = TextConflict(
            file_path="src/conflict.py",
            conflict_markers=3,
            base_lines=(40, 50),
            ours_lines=(45, 55),
            theirs_lines=(40, 48),
        )
        serialized = _serialize_evidence(original)
        deserialized = _deserialize_evidence(serialized)

        assert isinstance(deserialized, TextConflict)
        assert deserialized.file_path == original.file_path
        assert deserialized.conflict_markers == original.conflict_markers
        assert deserialized.base_lines == original.base_lines

    def test_unknown_evidence_kind_raises(self):
        """Deserializing unknown evidence kind raises ValueError."""
        with pytest.raises(ValueError, match="Unknown evidence kind: UnknownKind"):
            _deserialize_evidence({"_kind": "UnknownKind", "file_path": "test.py"})


class TestEventEmitters:
    """Tests for event emitter helpers."""

    def test_emit_integration_risk_scored(self, tmp_path, mock_emit):
        """Risk scored event emits with correct payload structure."""
        record = IntegrationRiskRecord(
            task_slug="task-123",
            target_head_sha="abc123",
            task_base_sha="def456",
            task_commit_sha="ghi789",
            risk_level=RiskLevel.HIGH,
            claimed_files=("src/a.py",),
            changed_files=("src/a.py",),
            python_overlap_detected=True,
            overlap_evidence=(),
            computed_at=0.0,
        )

        emit_integration_risk_scored(mock_emit, str(tmp_path), "test-plan", record)

        assert len(mock_emit.calls) == 1
        call = mock_emit.calls[0]
        assert call["event"] == "integration_risk_scored"
        assert call["pane"] == "semantic-settlement"
        assert call["kwargs"]["task_slug"] == "task-123"
        assert call["kwargs"]["risk_level"] == "high"

    def test_emit_integration_overlap_detected(self, tmp_path, mock_emit):
        """Overlap detected event emits with evidence."""
        evidence = SymbolOverlap(
            symbol_name="process",
            symbol_type="function",
            file_path="src/runner.py",
        )

        emit_integration_overlap_detected(
            mock_emit, str(tmp_path), "test-plan", "task-123", evidence
        )

        assert len(mock_emit.calls) == 1
        assert mock_emit.calls[0]["event"] == "integration_overlap_detected"
        assert mock_emit.calls[0]["kwargs"]["evidence"]["_kind"] == "SymbolOverlap"

    def test_emit_integration_candidate_passed(self, tmp_path, mock_emit):
        """Candidate passed event emits with verdict."""
        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=True,
        )

        emit_integration_candidate_passed(mock_emit, str(tmp_path), "test-plan", verdict)

        assert len(mock_emit.calls) == 1
        assert mock_emit.calls[0]["event"] == "integration_candidate_passed"
        assert mock_emit.calls[0]["kwargs"]["passed"] is True

    def test_emit_integration_candidate_failed(self, tmp_path, mock_emit):
        """Candidate failed event emits with failure details."""
        evidence = TextConflict(file_path="src/conflict.py", conflict_markers=2)
        verdict = IntegrationCandidateVerdict(
            task_slug="task-123",
            candidate_sha="candidate-abc",
            target_head_sha="target-def",
            passed=False,
            failure_class=FailureClass.TEXT_CONFLICT,
            evidence=(evidence,),
            error_message="Merge conflict",
        )

        emit_integration_candidate_failed(mock_emit, str(tmp_path), "test-plan", verdict)

        assert len(mock_emit.calls) == 1
        call = mock_emit.calls[0]
        assert call["event"] == "integration_candidate_failed"
        assert call["kwargs"]["failure_class"] == "text_conflict"
        assert call["kwargs"]["error_message"] == "Merge conflict"

    def test_emit_semantic_gate_rejected(self, tmp_path, mock_emit):
        """Semantic gate rejected event emits with gate details."""
        verdict = SemanticGateVerdict(
            task_slug="task-123",
            gate_name="same_symbol_edit",
            passed=False,
            failure_class=FailureClass.SAME_SYMBOL_EDIT,
            error_message="Both sides modified the function",
        )

        emit_semantic_gate_rejected(mock_emit, str(tmp_path), "test-plan", verdict)

        assert len(mock_emit.calls) == 1
        call = mock_emit.calls[0]
        assert call["event"] == "semantic_gate_rejected"
        assert call["kwargs"]["gate_name"] == "same_symbol_edit"
        assert call["kwargs"]["failure_class"] == "same_symbol_edit"


class TestPayloadDeserialization:
    """Tests for payload deserialization from event data."""

    def test_parse_integration_risk_record(self):
        """Parse risk record from event data dict."""
        event_data = {
            "task_slug": "task-123",
            "target_head_sha": "abc123",
            "task_base_sha": "def456",
            "task_commit_sha": "ghi789",
            "risk_level": "high",
            "claimed_files": ["src/a.py"],
            "changed_files": ["src/a.py"],
            "python_overlap_detected": True,
            "overlap_evidence": [],
            "ts": 1234567890.0,
        }

        record = parse_integration_risk_record(event_data)

        assert record.task_slug == "task-123"
        assert record.risk_level == RiskLevel.HIGH
        assert record.python_overlap_detected is True
        assert record.computed_at == 1234567890.0

    def test_parse_integration_risk_record_with_evidence(self):
        """Parse risk record with overlap evidence."""
        event_data = {
            "task_slug": "task-123",
            "target_head_sha": "abc123",
            "task_base_sha": "def456",
            "task_commit_sha": "ghi789",
            "risk_level": "high",
            "claimed_files": ["src/runner.py"],
            "changed_files": ["src/runner.py"],
            "python_overlap_detected": True,
            "overlap_evidence": [
                {
                    "_kind": "SymbolOverlap",
                    "symbol_name": "process",
                    "symbol_type": "function",
                    "file_path": "src/runner.py",
                    "task_line_range": [10, 20],
                    "target_line_range": [10, 25],
                }
            ],
        }

        record = parse_integration_risk_record(event_data)

        assert len(record.overlap_evidence) == 1
        ev = record.overlap_evidence[0]
        assert isinstance(ev, SymbolOverlap)
        assert ev.symbol_name == "process"
        assert ev.task_line_range == (10, 20)

    def test_parse_integration_candidate_verdict_passed(self):
        """Parse passed candidate verdict from event data."""
        event_data = {
            "task_slug": "task-123",
            "candidate_sha": "candidate-abc",
            "target_head_sha": "target-def",
            "passed": True,
            "failure_class": None,
            "evidence": [],
            "ts": 1234567890.0,
        }

        verdict = parse_integration_candidate_verdict(event_data)

        assert verdict.passed is True
        assert verdict.failure_class is None
        assert verdict.validated_at == 1234567890.0

    def test_parse_integration_candidate_verdict_failed(self):
        """Parse failed candidate verdict from event data."""
        event_data = {
            "task_slug": "task-123",
            "candidate_sha": "candidate-abc",
            "target_head_sha": "target-def",
            "passed": False,
            "failure_class": "text_conflict",
            "error_message": "Merge conflict",
            "evidence": [
                {
                    "_kind": "TextConflict",
                    "file_path": "src/conflict.py",
                    "conflict_markers": 2,
                }
            ],
        }

        verdict = parse_integration_candidate_verdict(event_data)

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.TEXT_CONFLICT
        assert len(verdict.evidence) == 1

    def test_parse_semantic_gate_verdict(self):
        """Parse semantic gate verdict from event data."""
        event_data = {
            "task_slug": "task-123",
            "gate_name": "same_symbol_edit",
            "passed": False,
            "failure_class": "same_symbol_edit",
            "error_message": "Both sides modified",
            "evidence": [
                {
                    "_kind": "SymbolOverlap",
                    "symbol_name": "process",
                    "symbol_type": "function",
                    "file_path": "src/worker.py",
                }
            ],
        }

        verdict = parse_semantic_gate_verdict(event_data)

        assert verdict.task_slug == "task-123"
        assert verdict.gate_name == "same_symbol_edit"
        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SAME_SYMBOL_EDIT


class TestImmutability:
    """Tests that dataclasses are frozen (immutable)."""

    def test_symbol_overlap_frozen(self):
        """SymbolOverlap is immutable after creation."""
        overlap = SymbolOverlap(
            symbol_name="test",
            symbol_type="function",
            file_path="test.py",
        )

        # Use a helper to suppress type checker while testing runtime behavior
        def _try_set(obj: Any) -> None:
            obj.symbol_name = "new_name"

        with pytest.raises(AttributeError):
            _try_set(overlap)

    def test_integration_risk_record_frozen(self):
        """IntegrationRiskRecord is immutable after creation."""
        record = IntegrationRiskRecord(
            task_slug="test",
            target_head_sha="abc",
            task_base_sha="def",
            task_commit_sha="ghi",
            risk_level=RiskLevel.LOW,
            claimed_files=(),
            changed_files=(),
        )

        # Use a helper to suppress type checker while testing runtime behavior
        def _try_set(obj: Any) -> None:
            obj.task_slug = "new"

        with pytest.raises(AttributeError):
            _try_set(record)

    def test_risk_level_is_strenum(self):
        """RiskLevel is a StrEnum, not just a string."""
        assert issubclass(RiskLevel, str)
        assert RiskLevel.HIGH == "high"
        assert str(RiskLevel.HIGH) == "high"
