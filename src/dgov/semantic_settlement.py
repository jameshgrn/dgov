"""Semantic Settlement Foundation — contract types and payload helpers.

Defines the failure taxonomy, risk levels, overlap evidence, and candidate
verdicts for integration-aware settlement. This module provides the shared
contract and payload-shaping helpers; it does not perform merge orchestration.

Event family:
- integration_risk_scored
- integration_overlap_detected
- integration_candidate_passed
- integration_candidate_failed
- semantic_gate_rejected
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

# -----------------------------------------------------------------------------
# Failure Taxonomy
# -----------------------------------------------------------------------------


class FailureClass(StrEnum):
    """Machine-readable failure taxonomy for integration conflicts."""

    TEXT_CONFLICT = "text_conflict"
    """Git cannot replay the task commit cleanly."""

    SYNTAX_CONFLICT = "syntax_conflict"
    """The integrated file no longer parses."""

    SAME_SYMBOL_EDIT = "same_symbol_edit"
    """Both sides changed the same Python symbol."""

    DUPLICATE_DEFINITION = "duplicate_definition"
    """Integrated Python code defines the same symbol twice."""

    SIGNATURE_DRIFT = "signature_drift"
    """A touched public callable changed shape relative to the task snapshot or target head."""

    ORDERING_CONFLICT = "ordering_conflict"
    """Operations are individually valid but invalid in the integrated order."""

    BEHAVIORAL_MISMATCH = "behavioral_mismatch"
    """Integrated candidate passes parse-level checks but fails settlement gates."""


# -----------------------------------------------------------------------------
# Risk Levels
# -----------------------------------------------------------------------------


class RiskLevel(StrEnum):
    """Integration risk classification for telemetry and review."""

    NONE = "none"
    """No detectable integration risk."""

    LOW = "low"
    """Minimal risk; standard settlement gates sufficient."""

    MEDIUM = "medium"
    """Elevated risk; shadow-mode validation recommended."""

    HIGH = "high"
    """Significant risk; integrated candidate validation required."""

    CRITICAL = "critical"
    """Near-certain conflict; reject without full integration attempt."""


# -----------------------------------------------------------------------------
# Overlap Evidence
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolOverlap:
    """Evidence of concurrent symbol edits between task and target."""

    symbol_name: str
    symbol_type: str  # 'function', 'class', 'method', 'variable', etc.
    file_path: str
    task_line_range: tuple[int, int] | None = None
    target_line_range: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class DuplicateDefinition:
    """Evidence of duplicate symbol definitions in integrated result."""

    symbol_name: str
    symbol_type: str
    file_paths: tuple[str, ...]  # Multiple files defining the same symbol
    line_numbers: tuple[tuple[int, int], ...] | None = None  # Ranges per file


@dataclass(frozen=True, slots=True)
class SignatureDrift:
    """Evidence of public callable signature changes."""

    symbol_name: str
    file_path: str
    base_signature: str  # Original signature (from task snapshot or target)
    integrated_signature: str  # Signature in integrated candidate


@dataclass(frozen=True, slots=True)
class SyntaxConflict:
    """Evidence of syntax-level integration failure."""

    file_path: str
    line_number: int | None = None
    column: int | None = None
    error_message: str = ""
    parser_used: str = "python"  # 'python', 'json', 'yaml', etc.


@dataclass(frozen=True, slots=True)
class TextConflict:
    """Evidence of Git-level text conflict."""

    file_path: str
    conflict_markers: int  # Count of conflict marker blocks
    base_lines: tuple[int, int] | None = None  # Line range in base
    ours_lines: tuple[int, int] | None = None  # Line range in task branch
    theirs_lines: tuple[int, int] | None = None  # Line range in target branch


# Union type for all overlap evidence kinds
OverlapEvidence = (
    SymbolOverlap | DuplicateDefinition | SignatureDrift | SyntaxConflict | TextConflict
)


# -----------------------------------------------------------------------------
# Integration Risk Record
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationRiskRecord:
    """Computed risk assessment for a task before landing.

    Captures the integration risk using current target HEAD, task base snapshot,
    task commit diff, declared file claims, and Python symbol overlap when relevant.
    """

    task_slug: str
    target_head_sha: str
    task_base_sha: str
    task_commit_sha: str
    risk_level: RiskLevel
    claimed_files: tuple[str, ...]
    changed_files: tuple[str, ...]
    python_overlap_detected: bool = False
    overlap_evidence: tuple[OverlapEvidence, ...] = ()
    computed_at: float = 0.0  # Timestamp; filled by emitter


# -----------------------------------------------------------------------------
# Candidate Verdicts
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationCandidateVerdict:
    """Outcome of integrated candidate validation.

    Result of building an ephemeral candidate workspace rooted at current target
    HEAD, replaying the task commit, and running settlement gates.
    """

    task_slug: str
    candidate_sha: str  # SHA of the ephemeral integrated commit
    target_head_sha: str  # Target HEAD at validation time
    passed: bool
    failure_class: FailureClass | None = None
    evidence: tuple[OverlapEvidence, ...] = ()
    error_message: str = ""
    validated_at: float = 0.0  # Timestamp; filled by emitter


@dataclass(frozen=True, slots=True)
class SemanticGateVerdict:
    """Outcome of deterministic Python semantic gate.

    Rejection includes precise reasons but does not attempt automatic resolution.
    """

    task_slug: str
    gate_name: str  # 'same_symbol_edit', 'duplicate_definition', 'signature_drift', etc.
    passed: bool
    failure_class: FailureClass | None = None
    evidence: tuple[OverlapEvidence, ...] = ()
    error_message: str = ""
    checked_at: float = 0.0  # Timestamp; filled by emitter


# -----------------------------------------------------------------------------
# Event Payload Helpers
# -----------------------------------------------------------------------------


# Registry: evidence type name -> dataclass type
_EVIDENCE_TYPES: dict[str, type[OverlapEvidence]] = {
    "SymbolOverlap": SymbolOverlap,
    "DuplicateDefinition": DuplicateDefinition,
    "SignatureDrift": SignatureDrift,
    "SyntaxConflict": SyntaxConflict,
    "TextConflict": TextConflict,
}

# Fields that should be tuples in evidence dataclasses
_TUPLE_FIELDS = frozenset({
    "task_line_range",
    "target_line_range",
    "file_paths",
    "line_numbers",
    "base_lines",
    "ours_lines",
    "theirs_lines",
})


def _to_tuple(value: Any, field: str) -> Any:
    """Convert list to tuple when needed for dataclass fields."""
    if not isinstance(value, list):
        return value
    if field == "line_numbers":
        return tuple(tuple(item) if isinstance(item, list) else item for item in value)
    if field in _TUPLE_FIELDS:
        return tuple(value)
    return value


def _serialize_evidence(evidence: OverlapEvidence) -> dict[str, Any]:
    """Convert an evidence dataclass to a JSON-serializable dict."""
    data = asdict(evidence)
    return {"_kind": evidence.__class__.__name__} | {
        k: list(v) if isinstance(v, tuple) else v for k, v in data.items()
    }


def _deserialize_evidence(data: dict[str, Any]) -> OverlapEvidence:
    """Reconstruct an evidence dataclass from a JSON-deserialized dict."""
    kind = data.pop("_kind", None)
    if kind not in _EVIDENCE_TYPES:
        raise ValueError(f"Unknown evidence kind: {kind}")
    # Convert lists back to tuples where needed
    converted = {k: _to_tuple(v, k) for k, v in data.items()}
    return _EVIDENCE_TYPES[kind](**converted)


def _evidence_payload(evidence: tuple[OverlapEvidence, ...]) -> list[dict[str, Any]]:
    """Convert evidence tuple to serialized list for payloads."""
    return [_serialize_evidence(e) for e in evidence]


def _fc_value(fc: FailureClass | None) -> str | None:
    """Convert optional FailureClass to its string value."""
    return fc.value if fc else None


def emit_integration_risk_scored(
    emit_fn,
    session_root: str,
    plan_name: str,
    record: IntegrationRiskRecord,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_risk_scored event with structured payload."""
    payload = {
        "task_slug": record.task_slug,
        "target_head_sha": record.target_head_sha,
        "task_base_sha": record.task_base_sha,
        "task_commit_sha": record.task_commit_sha,
        "risk_level": record.risk_level.value,
        "claimed_files": list(record.claimed_files),
        "changed_files": list(record.changed_files),
        "python_overlap_detected": record.python_overlap_detected,
        "overlap_evidence": _evidence_payload(record.overlap_evidence),
    }
    emit_fn(session_root, "integration_risk_scored", pane, plan_name=plan_name, **payload)


def emit_integration_overlap_detected(
    emit_fn,
    session_root: str,
    plan_name: str,
    task_slug: str,
    evidence: OverlapEvidence,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_overlap_detected event with evidence payload."""
    payload = {
        "task_slug": task_slug,
        "evidence": _serialize_evidence(evidence),
    }
    emit_fn(session_root, "integration_overlap_detected", pane, plan_name=plan_name, **payload)


def emit_integration_candidate_passed(
    emit_fn,
    session_root: str,
    plan_name: str,
    verdict: IntegrationCandidateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_candidate_passed event with verdict payload."""
    payload = {
        "task_slug": verdict.task_slug,
        "candidate_sha": verdict.candidate_sha,
        "target_head_sha": verdict.target_head_sha,
        "passed": verdict.passed,
        "evidence": _evidence_payload(verdict.evidence),
    }
    emit_fn(session_root, "integration_candidate_passed", pane, plan_name=plan_name, **payload)


def emit_integration_candidate_failed(
    emit_fn,
    session_root: str,
    plan_name: str,
    verdict: IntegrationCandidateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_candidate_failed event with verdict payload."""
    payload: dict[str, Any] = {
        "task_slug": verdict.task_slug,
        "candidate_sha": verdict.candidate_sha,
        "target_head_sha": verdict.target_head_sha,
        "passed": verdict.passed,
        "failure_class": _fc_value(verdict.failure_class),
        "error_message": verdict.error_message,
        "evidence": _evidence_payload(verdict.evidence),
    }
    emit_fn(session_root, "integration_candidate_failed", pane, plan_name=plan_name, **payload)


def emit_semantic_gate_rejected(
    emit_fn,
    session_root: str,
    plan_name: str,
    verdict: SemanticGateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit semantic_gate_rejected event with gate verdict payload."""
    payload: dict[str, Any] = {
        "task_slug": verdict.task_slug,
        "gate_name": verdict.gate_name,
        "passed": verdict.passed,
        "failure_class": _fc_value(verdict.failure_class),
        "error_message": verdict.error_message,
        "evidence": _evidence_payload(verdict.evidence),
    }
    emit_fn(session_root, "semantic_gate_rejected", pane, plan_name=plan_name, **payload)


# -----------------------------------------------------------------------------
# Payload Deserialization Helpers (for review tooling)
# -----------------------------------------------------------------------------


def _parse_evidence_list(data: dict[str, Any], key: str) -> tuple[OverlapEvidence, ...]:
    """Parse a list of evidence dicts from event data."""
    items = data.get(key, [])
    return tuple(_deserialize_evidence(e) for e in items) if items else ()


def _parse_failure_class(data: dict[str, Any]) -> FailureClass | None:
    """Parse optional failure_class from event data."""
    value = data.get("failure_class")
    return FailureClass(value) if value else None


def parse_integration_risk_record(event_data: dict[str, Any]) -> IntegrationRiskRecord:
    """Parse an integration_risk_scored event data dict into a typed record."""
    return IntegrationRiskRecord(
        task_slug=event_data["task_slug"],
        target_head_sha=event_data["target_head_sha"],
        task_base_sha=event_data["task_base_sha"],
        task_commit_sha=event_data["task_commit_sha"],
        risk_level=RiskLevel(event_data["risk_level"]),
        claimed_files=tuple(event_data.get("claimed_files", [])),
        changed_files=tuple(event_data.get("changed_files", [])),
        python_overlap_detected=event_data.get("python_overlap_detected", False),
        overlap_evidence=_parse_evidence_list(event_data, "overlap_evidence"),
        computed_at=event_data.get("ts", 0.0),
    )


def parse_integration_candidate_verdict(
    event_data: dict[str, Any],
) -> IntegrationCandidateVerdict:
    """Parse an integration_candidate_passed/failed event into a typed verdict."""
    return IntegrationCandidateVerdict(
        task_slug=event_data["task_slug"],
        candidate_sha=event_data["candidate_sha"],
        target_head_sha=event_data["target_head_sha"],
        passed=event_data["passed"],
        failure_class=_parse_failure_class(event_data),
        evidence=_parse_evidence_list(event_data, "evidence"),
        error_message=event_data.get("error_message", ""),
        validated_at=event_data.get("ts", 0.0),
    )


def parse_semantic_gate_verdict(event_data: dict[str, Any]) -> SemanticGateVerdict:
    """Parse a semantic_gate_rejected event into a typed verdict."""
    return SemanticGateVerdict(
        task_slug=event_data["task_slug"],
        gate_name=event_data["gate_name"],
        passed=event_data["passed"],
        failure_class=_parse_failure_class(event_data),
        evidence=_parse_evidence_list(event_data, "evidence"),
        error_message=event_data.get("error_message", ""),
        checked_at=event_data.get("ts", 0.0),
    )


# -----------------------------------------------------------------------------
# Module exports
# -----------------------------------------------------------------------------

__all__ = [
    "DuplicateDefinition",
    "FailureClass",
    "IntegrationCandidateVerdict",
    "IntegrationRiskRecord",
    "OverlapEvidence",
    "RiskLevel",
    "SemanticGateVerdict",
    "SignatureDrift",
    "SymbolOverlap",
    "SyntaxConflict",
    "TextConflict",
    "_deserialize_evidence",
    "_serialize_evidence",
    "emit_integration_candidate_failed",
    "emit_integration_candidate_passed",
    "emit_integration_overlap_detected",
    "emit_integration_risk_scored",
    "emit_semantic_gate_rejected",
    "parse_integration_candidate_verdict",
    "parse_integration_risk_record",
    "parse_semantic_gate_verdict",
]
