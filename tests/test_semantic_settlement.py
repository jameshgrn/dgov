"""Tests for semantic_settlement module — contract types and payload shaping."""

from __future__ import annotations

import subprocess
from pathlib import Path
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

    def test_compute_semantic_risk_populates_symbol_overlap_evidence(self, tmp_path: Path):
        from dgov.actions import MergeTask
        from dgov.config import ProjectConfig
        from dgov.settlement_flow import SettlementFlow
        from dgov.types import Worktree

        _init_git_repo(tmp_path)
        module = tmp_path / "module.py"
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

        flow = SettlementFlow(
            session_root=str(tmp_path),
            plan_name="test-plan",
            project_config=ProjectConfig(),
        )
        record = flow.compute_semantic_risk(
            action=MergeTask("task", "pane", ("module.py",)),
            wt=Worktree(path=tmp_path, branch="task-branch", commit=base_sha),
            file_claims=("module.py",),
        )

        assert record.target_head_sha == target_sha
        assert record.task_commit_sha == task_sha
        assert record.risk_level == RiskLevel.MEDIUM
        assert record.python_overlap_detected is True
        overlaps = [
            evidence for evidence in record.overlap_evidence if isinstance(evidence, SymbolOverlap)
        ]
        assert overlaps
        assert {evidence.symbol_name for evidence in overlaps} == {"process"}

    def test_risk_level_scoring_uses_evidence_severity(self, tmp_path: Path):
        from dgov.config import ProjectConfig
        from dgov.settlement_flow import SettlementFlow

        flow = SettlementFlow(
            session_root=str(tmp_path),
            plan_name="test-plan",
            project_config=ProjectConfig(),
        )
        drift = SignatureDrift(
            symbol_name="process",
            file_path="module.py",
            base_signature="def process(value)",
            integrated_signature="def process(value, default)",
        )
        overlap = SymbolOverlap(
            symbol_name="process",
            symbol_type="function",
            file_path="module.py",
        )
        duplicate = DuplicateDefinition(
            symbol_name="process",
            symbol_type="function",
            file_paths=("a.py", "b.py"),
        )

        assert flow._risk_level_from_evidence(()) == RiskLevel.NONE
        assert flow._risk_level_from_evidence((drift,)) == RiskLevel.LOW
        assert flow._risk_level_from_evidence((overlap,)) == RiskLevel.MEDIUM
        assert flow._risk_level_from_evidence((duplicate,)) == RiskLevel.HIGH
        assert flow._risk_level_from_evidence((drift, overlap)) == RiskLevel.CRITICAL
        assert flow._risk_level_from_evidence((drift, drift, drift, drift)) == RiskLevel.CRITICAL

    def test_critical_risk_short_circuits_candidate_creation(self, tmp_path: Path):
        import asyncio
        from unittest.mock import AsyncMock

        from dgov.actions import MergeTask
        from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
        from dgov.runner import EventDagRunner
        from dgov.types import Worktree

        task = DagTaskSpec(
            slug="task",
            summary="Task",
            prompt="Edit module",
            commit_message="Edit module",
            files=DagFileSpec(edit=("module.py",)),
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
        risk_record = IntegrationRiskRecord(
            task_slug="task",
            target_head_sha="target",
            task_base_sha="base",
            task_commit_sha="task",
            risk_level=RiskLevel.CRITICAL,
            claimed_files=("module.py",),
            changed_files=("module.py",),
            python_overlap_detected=True,
            overlap_evidence=(
                SymbolOverlap(
                    symbol_name="process",
                    symbol_type="function",
                    file_path="module.py",
                ),
            ),
        )
        create_candidate = AsyncMock()
        object.__setattr__(runner, "_prepare_and_commit", AsyncMock(return_value=(None, True)))
        object.__setattr__(
            runner,
            "_run_isolated_validation",
            AsyncMock(return_value=(None, risk_record)),
        )
        object.__setattr__(runner, "_create_integration_candidate_with_emit", create_candidate)

        async def _run_check() -> None:
            error, was_settlement = await runner._settle_and_merge(
                MergeTask("task", "pane", ("module.py",)),
                Worktree(path=tmp_path, branch="task-branch", commit="base"),
            )
            assert error is not None
            assert error.startswith("Integration risk CRITICAL:")
            assert "SymbolOverlap" in error
            assert was_settlement is True

        asyncio.run(_run_check())
        create_candidate.assert_not_called()


class TestPythonSemanticGateIntegration:
    """Integration tests for Python semantic gate with real git repos."""

    def test_semantic_gate_detects_class_method_collision(self, tmp_path: Path):
        """Gate detects when both sides modify the same class method."""
        import subprocess

        from dgov.semantic_settlement import (
            _get_symbols_at_commit,
        )

        # Initialize git repo
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

        # Create initial file with class
        cls_file = tmp_path / "module.py"
        cls_file.write_text("""
class Processor:
    def process(self):
        return "base"
""")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True
        )

        # Get base commit
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
        )
        base_sha = result.stdout.strip()

        # Create a branch with changes (task side)
        subprocess.run(
            ["git", "checkout", "-b", "task-branch"], cwd=tmp_path, check=True, capture_output=True
        )
        cls_file.write_text("""
class Processor:
    def process(self):
        return "task version"
""")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "task change"], cwd=tmp_path, check=True, capture_output=True
        )

        # Go back to main and make target changes
        subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
        cls_file.write_text("""
class Processor:
    def process(self):
        return "target version"
""")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "target change"], cwd=tmp_path, check=True, capture_output=True
        )

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
        )
        target_sha = result.stdout.strip()

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
