"""Tests for semantic_settlement module — contract types and payload shaping."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from dgov.dag_parser import DagTaskSpec
    from dgov.runner import EventDagRunner

from dgov.semantic_settlement import (
    DuplicateDefinition,
    FailureClass,
    IntegrationCandidateVerdict,
    IntegrationRiskRecord,
    OverlapEvidence,
    RiskLevel,
    SemanticGateVerdict,
    SignatureDrift,
    SymbolOverlap,
    SyntaxConflict,
    TextConflict,
    _deserialize_evidence,
    _serialize_evidence,
    describe_evidence,
    describe_evidence_payload,
    emit_integration_candidate_failed,
    emit_integration_candidate_passed,
    emit_integration_overlap_detected,
    emit_integration_risk_scored,
    emit_semantic_gate_rejected,
    evidence_payload,
    parse_evidence_payload,
    parse_integration_candidate_verdict,
    parse_integration_risk_record,
    parse_semantic_gate_verdict,
    summarize_evidence,
)

pytestmark = pytest.mark.unit


class MockEmit:
    """Callable mock that captures emit calls for verification."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, session_root: str, event: Any) -> None:
        self.calls.append((session_root, event))


@pytest.fixture
def mock_emit():
    """Create a mock emit function that captures calls."""
    return MockEmit()


_EvidenceCase = tuple[tuple[OverlapEvidence, ...], RiskLevel]


def _signature_drift(symbol_name: str) -> SignatureDrift:
    return SignatureDrift(
        symbol_name=symbol_name,
        file_path="module.py",
        base_signature=f"def {symbol_name}(value)",
        integrated_signature=f"def {symbol_name}(value, default)",
    )


def _symbol_overlap(symbol_name: str) -> SymbolOverlap:
    return SymbolOverlap(
        symbol_name=symbol_name,
        symbol_type="function",
        file_path="module.py",
    )


def _duplicate_definition(symbol_name: str) -> DuplicateDefinition:
    return DuplicateDefinition(
        symbol_name=symbol_name,
        symbol_type="function",
        file_paths=("a.py", "b.py"),
    )


def _risk_level_evidence_cases() -> tuple[_EvidenceCase, ...]:
    private_drift = _signature_drift("_process")
    public_drift = _signature_drift("process")
    private_overlap = _symbol_overlap("_process")
    public_overlap = _symbol_overlap("process")
    private_duplicate = _duplicate_definition("_process")
    public_duplicate = _duplicate_definition("process")
    syntax = SyntaxConflict(file_path="module.py", error_message="invalid syntax")
    text = TextConflict(file_path="module.py", conflict_markers=2)

    return (
        ((), RiskLevel.NONE),
        ((private_drift,), RiskLevel.LOW),
        ((public_drift,), RiskLevel.HIGH),
        ((private_overlap,), RiskLevel.MEDIUM),
        ((public_overlap,), RiskLevel.HIGH),
        ((private_duplicate,), RiskLevel.MEDIUM),
        ((public_duplicate,), RiskLevel.HIGH),
        ((syntax,), RiskLevel.CRITICAL),
        ((text,), RiskLevel.CRITICAL),
        ((private_drift, private_drift, private_drift, private_drift), RiskLevel.LOW),
        ((private_drift, public_overlap), RiskLevel.HIGH),
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# Fixtures for class-method collision testing in semantic gate
_PROCESSOR_CLASS_BASE = """
class Processor:
    def process(self):
        return "base"
"""

_PROCESSOR_CLASS_TASK = """
class Processor:
    def process(self):
        return "task version"
"""

_PROCESSOR_CLASS_TARGET = """
class Processor:
    def process(self):
        return "target version"
"""


def _setup_collision_repo_with_class_method(
    repo: Path,
    module_name: str = "module.py",
) -> tuple[str, str, str]:
    """Set up a repo with class method collision between task and target.

    Creates base commit with Processor class, then diverges:
    - task-branch: modifies process() to return "task version"
    - main (target): modifies process() to return "target version"

    Returns (base_sha, task_head_sha, target_head_sha) for symbol comparison.
    """
    # Initialize repo
    _init_git_repo(repo)

    # Create initial file with base class
    cls_file = repo / module_name
    cls_file.write_text(_PROCESSOR_CLASS_BASE)
    base_sha = _commit_all(repo, "initial")

    # Create task branch with changes
    _git(repo, "checkout", "-b", "task-branch")
    cls_file.write_text(_PROCESSOR_CLASS_TASK)
    task_sha = _commit_all(repo, "task change")

    # Return to main and make target changes
    _git(repo, "checkout", "main")
    cls_file.write_text(_PROCESSOR_CLASS_TARGET)
    target_sha = _commit_all(repo, "target change")

    return base_sha, task_sha, target_sha


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

    def test_evidence_payload_serializes_tuple(self):
        """Public payload helper serializes evidence tuples for subprocess output."""
        payload = evidence_payload((
            SymbolOverlap(
                symbol_name="process",
                symbol_type="function",
                file_path="src/runner.py",
            ),
        ))

        assert payload == [
            {
                "_kind": "SymbolOverlap",
                "symbol_name": "process",
                "symbol_type": "function",
                "file_path": "src/runner.py",
                "task_line_range": None,
                "target_line_range": None,
            }
        ]

    def test_parse_evidence_payload_does_not_mutate_input(self):
        """Public parser should not consume the serialized _kind field."""
        payload = [
            {
                "_kind": "TextConflict",
                "file_path": "src/conflict.py",
                "conflict_markers": 2,
                "base_lines": None,
                "ours_lines": None,
                "theirs_lines": None,
            }
        ]

        parsed = parse_evidence_payload(payload)

        assert isinstance(parsed[0], TextConflict)
        assert payload[0]["_kind"] == "TextConflict"

    def test_describe_evidence_formats_settlement_narrative(self):
        """Evidence descriptions should be readable by review, watch, and retry prompts."""
        evidence = SymbolOverlap(
            symbol_name="Processor.process",
            symbol_type="method",
            file_path="src/runner.py",
            task_line_range=(12, 18),
            target_line_range=(14, 22),
        )

        description = describe_evidence(evidence)

        assert description == (
            "same-symbol edit: method Processor.process in src/runner.py "
            "(task lines 12-18; target lines 14-22)"
        )

    def test_describe_evidence_payload_handles_serialized_records(self):
        """Serialized evidence should share the same narrative wording."""
        payload = evidence_payload((
            SignatureDrift(
                symbol_name="run",
                file_path="src/runner.py",
                base_signature="def run(path)",
                integrated_signature="def run(path, *, force)",
            ),
        ))

        assert describe_evidence_payload(payload) == (
            "signature drift: run in src/runner.py changed from def run(path) "
            "to def run(path, *, force)",
        )
        assert (
            summarize_evidence(parse_evidence_payload(payload))
            == describe_evidence_payload(payload)[0]
        )


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
        _root, evt = mock_emit.calls[0]
        assert evt.event_type == "integration_risk_scored"
        assert evt.pane == "semantic-settlement"
        assert evt.task_slug == "task-123"
        assert evt.risk_level == "high"

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
        _root, evt = mock_emit.calls[0]
        assert evt.event_type == "integration_overlap_detected"
        assert evt.evidence["_kind"] == "SymbolOverlap"

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
        _root, evt = mock_emit.calls[0]
        assert evt.event_type == "integration_candidate_passed"
        assert evt.passed is True

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
        _root, evt = mock_emit.calls[0]
        assert evt.event_type == "integration_candidate_failed"
        assert evt.failure_class == "text_conflict"
        assert evt.error_message == "Merge conflict"

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
        _root, evt = mock_emit.calls[0]
        assert evt.event_type == "semantic_gate_rejected"
        assert evt.gate_name == "same_symbol_edit"
        assert evt.failure_class == "same_symbol_edit"


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


class TestPythonSemanticAnalyzers:
    """Tests for deterministic Python semantic gate analyzers."""

    def test_analyze_python_file_symbols_extracts_functions(self, tmp_path: Path):
        """Analyzer extracts function symbols from Python file."""
        from dgov.semantic_settlement import _analyze_python_file_symbols

        test_file = tmp_path / "test.py"
        test_file.write_text("""
def foo():
    pass

def bar(x: int) -> str:
    return str(x)

async def baz():
    pass
""")

        symbols = _analyze_python_file_symbols(test_file)

        assert "foo" in symbols
        assert "bar" in symbols
        assert "baz" in symbols
        assert symbols["foo"].symbol_type == "function"
        assert symbols["bar"].symbol_type == "function"
        assert "def bar(x)" in (symbols["bar"].signature or "")

    def test_analyze_python_file_symbols_extracts_classes_and_methods(self, tmp_path: Path):
        """Analyzer extracts class and method symbols."""
        from dgov.semantic_settlement import _analyze_python_file_symbols

        test_file = tmp_path / "test.py"
        test_file.write_text("""
class MyClass:
    def method1(self):
        pass

    def method2(self, x: int):
        return x
""")

        symbols = _analyze_python_file_symbols(test_file)

        assert "MyClass" in symbols
        assert symbols["MyClass"].symbol_type == "class"
        assert "MyClass.method1" in symbols
        assert "MyClass.method2" in symbols
        assert symbols["MyClass.method1"].symbol_type == "method"

    def test_check_duplicate_definitions_detects_duplicates(self, tmp_path: Path):
        """Duplicate definition check finds same symbol in multiple files."""
        from dgov.semantic_settlement import _check_duplicate_definitions

        file1 = tmp_path / "a.py"
        file1.write_text("def shared(): pass\n")

        file2 = tmp_path / "b.py"
        file2.write_text("def shared(): pass\n")

        dups = _check_duplicate_definitions([file1, file2])

        assert len(dups) == 1
        assert dups[0].symbol_name == "shared"
        assert len(dups[0].file_paths) == 2

    def test_check_same_symbol_edit_detects_concurrent_edits(self):
        """Same symbol edit check finds overlapping changes."""
        from dgov.semantic_settlement import _check_same_symbol_edit, _SymbolInfo

        # Task changed foo (line range changed from base)
        task_base_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 5, "def foo()"),
        }
        task_commit_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 10, "def foo()"),
        }
        # Target also changed foo (signature changed from base)
        target_head_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 5, 15, "def foo(x)"),
        }

        overlaps = _check_same_symbol_edit(
            task_base_symbols, task_commit_symbols, target_head_symbols, {"src/a.py"}
        )

        assert len(overlaps) == 1
        assert overlaps[0].symbol_name == "foo"

    def test_check_same_symbol_edit_passes_one_sided_cleanup(self):
        """One-sided cleanup/refactor passes - only task changed the symbol."""
        from dgov.semantic_settlement import _check_same_symbol_edit, _SymbolInfo

        # Base state
        task_base_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 10, "def foo()"),
            "bar": _SymbolInfo("bar", "function", "src/a.py", 12, 20, "def bar()"),
        }
        # Task changed only foo (cleanup)
        task_commit_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 8, "def foo()"),  # Shortened
            "bar": _SymbolInfo("bar", "function", "src/a.py", 12, 20, "def bar()"),
        }
        # Target did not change anything (same as base)
        target_head_symbols = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 10, "def foo()"),
            "bar": _SymbolInfo("bar", "function", "src/a.py", 12, 20, "def bar()"),
        }

        overlaps = _check_same_symbol_edit(
            task_base_symbols, task_commit_symbols, target_head_symbols, {"src/a.py"}
        )

        # No overlap - only task changed, target didn't
        assert len(overlaps) == 0

    def test_check_signature_drift_detects_changes(self):
        """Signature drift check finds changed function signatures."""
        from dgov.semantic_settlement import _check_signature_drift, _SymbolInfo

        base = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 5, "def foo()"),
        }
        integrated = {
            "foo": _SymbolInfo("foo", "function", "src/a.py", 1, 5, "def foo(x: int)"),
        }

        drifts = _check_signature_drift(base, integrated, {"src/a.py"})

        assert len(drifts) == 1
        assert drifts[0].symbol_name == "foo"

    def test_run_python_semantic_gate_passes_non_python_files(self):
        """Non-Python tasks bypass the semantic gate."""
        from dgov.semantic_settlement import run_python_semantic_gate

        verdict = run_python_semantic_gate(
            candidate_path=Path("/tmp"),
            project_root="/tmp",
            task_base_sha="abc",
            task_commit_sha=None,  # Optional for non-Python
            target_head_sha="def",
            touched_files=("readme.md", "config.yaml"),
            task_slug="task-1",
        )

        assert verdict.passed is True

    def test_run_python_semantic_gate_detects_syntax_errors(self, tmp_path: Path):
        """Semantic gate fails closed on syntax errors."""
        from dgov.semantic_settlement import FailureClass, run_python_semantic_gate

        bad_file = tmp_path / "bad.py"
        bad_file.write_text("def broken(:")

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha="abc",
            task_commit_sha=None,  # Syntax error detected before symbol comparison
            target_head_sha="def",
            touched_files=("bad.py",),
            task_slug="task-1",
        )

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SYNTAX_CONFLICT

    def test_check_same_symbol_edit_true_conflict_rejects(self):
        """True concurrent edit to same symbol must still reject."""
        from dgov.semantic_settlement import _check_same_symbol_edit, _SymbolInfo

        # Base: original symbol definition
        task_base_symbols = {
            "process": _SymbolInfo(
                "process", "function", "src/worker.py", 10, 20, "def process()"
            ),
        }
        # Task side: modified the symbol (line range changed)
        task_commit_symbols = {
            "process": _SymbolInfo(
                "process", "function", "src/worker.py", 10, 25, "def process()"
            ),
        }
        # Target side: also modified the same symbol (signature changed)
        target_head_symbols = {
            "process": _SymbolInfo(
                "process", "function", "src/worker.py", 10, 20, "def process(item)"
            ),
        }

        overlaps = _check_same_symbol_edit(
            task_base_symbols, task_commit_symbols, target_head_symbols, {"src/worker.py"}
        )

        assert len(overlaps) == 1
        assert overlaps[0].symbol_name == "process"

    def test_check_same_symbol_edit_target_unchanged_passes(self):
        """Task-only edit (target unchanged) should pass - not a concurrent conflict."""
        from dgov.semantic_settlement import _check_same_symbol_edit, _SymbolInfo

        # Base state
        task_base_symbols = {
            "cleanup": _SymbolInfo("cleanup", "function", "src/module.py", 1, 10, "def cleanup()"),
        }
        # Task: symbol was modified (cleanup refactor)
        task_commit_symbols = {
            "cleanup": _SymbolInfo("cleanup", "function", "src/module.py", 1, 15, "def cleanup()"),
        }
        # Target: symbol unchanged (same as base)
        target_head_symbols = {
            "cleanup": _SymbolInfo("cleanup", "function", "src/module.py", 1, 10, "def cleanup()"),
        }

        overlaps = _check_same_symbol_edit(
            task_base_symbols, task_commit_symbols, target_head_symbols, {"src/module.py"}
        )

        # No concurrent conflict - only task changed it
        assert len(overlaps) == 0

    def test_check_duplicate_definitions_skips_test_files(self):
        """Duplicate symbols in test files should not trigger rejection."""
        import tempfile

        from dgov.semantic_settlement import _check_duplicate_definitions

        with tempfile.TemporaryDirectory() as tmp:
            # Create test files with same helper function (legitimate duplication)
            test_dir = Path(tmp) / "tests"
            test_dir.mkdir()

            test_file1 = test_dir / "test_a.py"
            test_file1.write_text("def helper(): return 1")

            test_file2 = test_dir / "test_b.py"
            test_file2.write_text("def helper(): return 2")

            dups = _check_duplicate_definitions([test_file1, test_file2])

            # Test file duplicates should be skipped
            assert len(dups) == 0

    def test_check_duplicate_definitions_allows_test_helpers(self):
        """Common test helper names should not trigger duplicate detection."""
        import tempfile

        from dgov.semantic_settlement import _check_duplicate_definitions

        with tempfile.TemporaryDirectory() as tmp:
            # Production code file
            src_file = Path(tmp) / "src.py"
            src_file.write_text("def test_helper(): return 'production'")

            # Test file with same name (test_helper is a common pattern)
            test_file = Path(tmp) / "test_foo.py"
            test_file.write_text("def test_helper(): return 'test'")

            dups = _check_duplicate_definitions([src_file, test_file])

            # test_* pattern duplicates should be allowed
            assert len(dups) == 0

    def test_check_duplicate_definitions_catches_production_dups(self):
        """Duplicate symbols in production code should still be detected."""
        import tempfile

        from dgov.semantic_settlement import _check_duplicate_definitions

        with tempfile.TemporaryDirectory() as tmp:
            file1 = Path(tmp) / "module_a.py"
            file1.write_text("def production_helper(): return 1")

            file2 = Path(tmp) / "module_b.py"
            file2.write_text("def production_helper(): return 2")

            dups = _check_duplicate_definitions([file1, file2])

            # Production code duplicates should be detected
            assert len(dups) == 1
            assert dups[0].symbol_name == "production_helper"


class TestSettlementFlowSemanticRisk:
    """Tests for Phase 2 semantic risk computation before integration."""

    def _setup_test_runner(
        self,
        tmp_path: Path,
        file_claims: tuple[str, ...],
    ) -> tuple[DagTaskSpec, EventDagRunner]:
        """Create a DagTaskSpec and EventDagRunner for semantic risk tests."""
        from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
        from dgov.runner import EventDagRunner

        task = DagTaskSpec(
            slug="task",
            summary="Task",
            prompt="Edit module",
            commit_message="Edit module",
            files=DagFileSpec(edit=file_claims),
        )
        runner = EventDagRunner(
            DagDefinition(
                name="test-plan",
                dag_file="test.toml",
                project_root=str(tmp_path),
                session_root=str(tmp_path),
                tasks={"task": task},
            ),
            session_root=str(tmp_path),
            restart=True,
        )
        return task, runner

    def _create_critical_risk_record(
        self,
        task_slug: str,
        file_claims: tuple[str, ...],
    ) -> IntegrationRiskRecord:
        """Create an IntegrationRiskRecord with CRITICAL risk level."""
        return IntegrationRiskRecord(
            task_slug=task_slug,
            target_head_sha="target",
            task_base_sha="base",
            task_commit_sha="task",
            risk_level=RiskLevel.CRITICAL,
            claimed_files=file_claims,
            changed_files=file_claims,
            python_overlap_detected=True,
            overlap_evidence=(
                SymbolOverlap(
                    symbol_name="process",
                    symbol_type="function",
                    file_path=file_claims[0],
                ),
            ),
        )

    def _patch_settlement_flow(
        self,
        runner: EventDagRunner,
        risk_record: IntegrationRiskRecord,
    ) -> AsyncMock:
        """Patch settlement flow methods and return the mocked create_integration_candidate."""
        from unittest.mock import AsyncMock

        create_candidate = AsyncMock()
        sf = runner._settlement_flow
        object.__setattr__(sf, "prepare_and_commit", AsyncMock(return_value=(None, True)))
        object.__setattr__(
            sf,
            "run_isolated_validation",
            AsyncMock(return_value=(None, risk_record)),
        )
        object.__setattr__(sf, "create_integration_candidate_with_emit", create_candidate)
        return create_candidate

    def _setup_symbol_overlap_repo(
        self,
        tmp_path: Path,
        module_name: str = "module.py",
    ) -> tuple[str, str, str]:
        """Initialize git repo with base, task, and target commits for symbol overlap testing.

        Returns (base_sha, task_sha, target_sha) where:
        - base: initial file with base process() function
        - task: branch with modified process() body
        - target: main branch with modified process() signature
        """
        _init_git_repo(tmp_path)
        module = tmp_path / module_name
        module.write_text("""
def process(value):
    return value
""")
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        module.write_text("""
def process(value):
    cleaned = value.strip()
    return cleaned
""")
        task_sha = _commit_all(tmp_path, "task change")

        _git(tmp_path, "checkout", "main")
        module.write_text("""
def process(value, default):
    return value or default
""")
        target_sha = _commit_all(tmp_path, "target change")

        return base_sha, task_sha, target_sha

    def _make_settlement_flow(self, tmp_path: Path):
        """Create a SettlementFlow instance for testing."""
        from dgov.config import ProjectConfig
        from dgov.settlement_flow import SettlementFlow

        return SettlementFlow(
            session_root=str(tmp_path),
            plan_name="test-plan",
            project_config=ProjectConfig(),
        )

    def test_compute_semantic_risk_populates_symbol_overlap_evidence(self, tmp_path: Path):
        from dgov.actions import MergeTask
        from dgov.types import Worktree

        base_sha, task_sha, target_sha = self._setup_symbol_overlap_repo(tmp_path)
        flow = self._make_settlement_flow(tmp_path)

        record = flow.compute_semantic_risk(
            action=MergeTask("task", "pane", ("module.py",)),
            wt=Worktree(path=tmp_path, branch="task-branch", commit=base_sha),
            file_claims=("module.py",),
        )

        assert record.target_head_sha == target_sha
        assert record.task_commit_sha == task_sha
        assert record.risk_level == RiskLevel.HIGH
        assert record.python_overlap_detected is True
        overlaps = [
            evidence for evidence in record.overlap_evidence if isinstance(evidence, SymbolOverlap)
        ]
        assert overlaps
        assert {evidence.symbol_name for evidence in overlaps} == {"process"}

    def test_compute_semantic_risk_decodes_unicode_changed_path(self, tmp_path: Path):
        from dgov.actions import MergeTask
        from dgov.types import Worktree

        name = "caf\u00e9.py"
        base_sha, task_sha, _target_sha = self._setup_symbol_overlap_repo(
            tmp_path,
            module_name=name,
        )
        flow = self._make_settlement_flow(tmp_path)

        record = flow.compute_semantic_risk(
            action=MergeTask("task", "pane", (name,)),
            wt=Worktree(path=tmp_path, branch="task-branch", commit=base_sha),
            file_claims=(name,),
        )

        assert record.task_commit_sha == task_sha
        assert record.changed_files == (name,)
        assert record.risk_level == RiskLevel.HIGH
        assert record.python_overlap_detected is True

    def test_risk_level_scoring_uses_evidence_severity(self, tmp_path: Path):
        flow = self._make_settlement_flow(tmp_path)

        for evidence, expected in _risk_level_evidence_cases():
            assert flow._risk_level_from_evidence(evidence) == expected

    def test_semantic_gate_rejection_message_includes_evidence(self, tmp_path: Path):
        from dgov.config import ProjectConfig
        from dgov.settlement_flow import SettlementFlow

        flow = SettlementFlow(
            session_root=str(tmp_path),
            plan_name="test-plan",
            project_config=ProjectConfig(),
        )
        verdict = SemanticGateVerdict(
            task_slug="task",
            gate_name="same_symbol_edit",
            passed=False,
            failure_class=FailureClass.SAME_SYMBOL_EDIT,
            evidence=(
                SymbolOverlap(
                    symbol_name="foo",
                    symbol_type="function",
                    file_path="src/a.py",
                ),
            ),
            error_message="concurrent edit detected",
        )

        message = flow.semantic_gate_rejection_message(verdict)

        assert "Semantic gate 'same_symbol_edit' rejected: same_symbol_edit" in message
        assert "concurrent edit detected" in message
        assert "Settlement evidence:" in message
        assert "same-symbol edit: function foo in src/a.py" in message

    def test_candidate_failure_message_includes_text_conflict_evidence(self, tmp_path: Path):
        from dgov.config import ProjectConfig
        from dgov.settlement_flow import SettlementFlow
        from dgov.worktree import IntegrationCandidateResult

        flow = SettlementFlow(
            session_root=str(tmp_path),
            plan_name="test-plan",
            project_config=ProjectConfig(),
        )
        result = IntegrationCandidateResult(
            passed=False,
            error="Replay failed",
            conflict_files=("src/a.py",),
            conflict_marker_counts={"src/a.py": 2},
        )

        message = flow.integration_candidate_failure_message(result)

        assert "Replay failed" in message
        assert "Settlement evidence:" in message
        assert "text conflict: src/a.py has 2 conflict marker blocks" in message

    def test_critical_risk_short_circuits_candidate_creation(self, tmp_path: Path):
        import asyncio

        from dgov.actions import MergeTask
        from dgov.types import Worktree

        file_claims = ("module.py",)
        task, runner = self._setup_test_runner(tmp_path, file_claims)
        risk_record = self._create_critical_risk_record(task.slug, file_claims)
        create_candidate = self._patch_settlement_flow(runner, risk_record)

        async def _run_check() -> None:
            error, was_settlement = await runner._settle_and_merge(
                MergeTask("task", "pane", file_claims),
                Worktree(path=tmp_path, branch="task-branch", commit="base"),
            )
            assert error is not None
            assert error.startswith("Integration risk CRITICAL:")
            assert "same-symbol edit: function process in module.py" in error
            assert was_settlement is True

        asyncio.run(_run_check())
        create_candidate.assert_not_called()


class TestPythonSemanticGateIntegration:
    """Integration tests for Python semantic gate with real git repos."""

    def test_semantic_gate_checks_unicode_python_diff(self, tmp_path: Path):
        from dgov.semantic_settlement import FailureClass, run_python_semantic_gate

        name = "caf\u00e9.py"
        _init_git_repo(tmp_path)
        (tmp_path / name).write_text("x = 1\n")
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        (tmp_path / name).write_text("def broken(\n")
        task_sha = _commit_all(tmp_path, "break unicode path")

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha=base_sha,
            task_commit_sha=task_sha,
            target_head_sha=base_sha,
            touched_files=(),
            task_slug="task",
        )

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SYNTAX_CONFLICT
        assert verdict.evidence
        assert isinstance(verdict.evidence[0], SyntaxConflict)
        assert verdict.evidence[0].file_path == name

    def test_semantic_gate_detects_class_method_collision(self, tmp_path: Path):
        """Gate detects when both sides modify the same class method."""
        from dgov.semantic_settlement import (
            _get_symbols_at_commit,
        )

        base_sha, _task_sha, target_sha = _setup_collision_repo_with_class_method(tmp_path)

        # Get symbols at both commits
        task_symbols = _get_symbols_at_commit(str(tmp_path), base_sha, ["module.py"])
        target_symbols = _get_symbols_at_commit(str(tmp_path), target_sha, ["module.py"])

        # Both should have the method
        assert "Processor.process" in task_symbols
        assert "Processor.process" in target_symbols

    def test_semantic_gate_finds_duplicate_definitions(self, tmp_path: Path):
        """Gate detects duplicate function definitions in integrated result."""
        from dgov.semantic_settlement import (
            _check_duplicate_definitions,
        )

        # Create two files with same function name (duplicate across files)
        file1 = tmp_path / "a.py"
        file1.write_text("def helper(): return 1")

        file2 = tmp_path / "b.py"
        file2.write_text("def helper(): return 2")

        dups = _check_duplicate_definitions([file1, file2])

        # Should detect cross-file duplicate
        assert len(dups) == 1
        assert dups[0].symbol_name == "helper"

    def test_semantic_gate_rejects_duplicate_from_target_changed_file(self, tmp_path: Path):
        """Gate includes target-changed Python files in cross-file duplicate checks."""
        from dgov.semantic_settlement import FailureClass, run_python_semantic_gate

        _init_git_repo(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        task_file = src / "task.py"
        target_file = src / "target.py"
        task_base = "def task_only():\n    return 1\n"
        task_changed = task_base + "\n\ndef shared():\n    return 'task'\n"
        target_changed = "def shared():\n    return 'target'\n"

        task_file.write_text(task_base)
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        task_file.write_text(task_changed)
        task_sha = _commit_all(tmp_path, "task adds shared")

        _git(tmp_path, "checkout", "main")
        target_file.write_text(target_changed)
        target_sha = _commit_all(tmp_path, "target adds shared")
        task_file.write_text(task_changed)

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha=base_sha,
            task_commit_sha=task_sha,
            target_head_sha=target_sha,
            touched_files=("src/task.py",),
            task_slug="task",
        )

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.DUPLICATE_DEFINITION
        assert len(verdict.evidence) == 1
        duplicate = verdict.evidence[0]
        assert isinstance(duplicate, DuplicateDefinition)
        assert duplicate.symbol_name == "shared"
        assert set(duplicate.file_paths) == {"src/task.py", "src/target.py"}

    def test_semantic_gate_ignores_preexisting_cross_file_duplicates(self, tmp_path: Path):
        """Pre-existing duplicate names across files are not a new settlement conflict."""
        from dgov.semantic_settlement import run_python_semantic_gate

        _init_git_repo(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        task_file = src / "task.py"
        target_file = src / "target.py"
        task_base = "def shared():\n    return 'base task'\n"
        target_base = "def shared():\n    return 'base target'\n"
        task_changed = "def shared():\n    return 'task'\n"
        target_changed = "def shared():\n    return 'target'\n"

        task_file.write_text(task_base)
        target_file.write_text(target_base)
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        task_file.write_text(task_changed)
        task_sha = _commit_all(tmp_path, "task edits task module")

        _git(tmp_path, "checkout", "main")
        target_file.write_text(target_changed)
        target_sha = _commit_all(tmp_path, "target edits target module")
        task_file.write_text(task_changed)

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha=base_sha,
            task_commit_sha=task_sha,
            target_head_sha=target_sha,
            touched_files=("src/task.py",),
            task_slug="task",
        )

        assert verdict.passed is True

    def test_semantic_gate_ignores_target_head_duplicate_unrelated_to_task(self, tmp_path: Path):
        """Target-head duplicate names should not be charged to an unrelated task."""
        from dgov.semantic_settlement import run_python_semantic_gate

        _init_git_repo(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        target_a = src / "target_a.py"
        target_b = src / "target_b.py"
        task_file = src / "task.py"
        target_a.write_text("def alpha():\n    return 1\n")
        target_b.write_text("def beta():\n    return 2\n")
        task_file.write_text("def task_only():\n    return 3\n")
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        task_file.write_text("def task_only():\n    return 4\n")
        task_sha = _commit_all(tmp_path, "task edits task module")

        _git(tmp_path, "checkout", "main")
        target_a.write_text("def shared():\n    return 1\n")
        target_b.write_text("def shared():\n    return 2\n")
        target_sha = _commit_all(tmp_path, "target has duplicate")
        task_file.write_text("def task_only():\n    return 4\n")

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha=base_sha,
            task_commit_sha=task_sha,
            target_head_sha=target_sha,
            touched_files=("src/task.py",),
            task_slug="task",
        )

        assert verdict.passed is True

    def test_semantic_gate_ignores_target_head_syntax_error_unrelated_to_task(
        self, tmp_path: Path
    ):
        """Syntax rejection stays scoped to files changed by the task."""
        from dgov.semantic_settlement import run_python_semantic_gate

        _init_git_repo(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        target_file = src / "target.py"
        task_file = src / "task.py"
        target_file.write_text("def target_only():\n    return 1\n")
        task_file.write_text("def task_only():\n    return 2\n")
        base_sha = _commit_all(tmp_path, "base")

        _git(tmp_path, "checkout", "-b", "task-branch")
        task_file.write_text("def task_only():\n    return 3\n")
        task_sha = _commit_all(tmp_path, "task edits task module")

        _git(tmp_path, "checkout", "main")
        target_file.write_text("def broken(:\n")
        target_sha = _commit_all(tmp_path, "target has syntax error")
        task_file.write_text("def task_only():\n    return 3\n")

        verdict = run_python_semantic_gate(
            candidate_path=tmp_path,
            project_root=str(tmp_path),
            task_base_sha=base_sha,
            task_commit_sha=task_sha,
            target_head_sha=target_sha,
            touched_files=("src/task.py",),
            task_slug="task",
        )

        assert verdict.passed is True
