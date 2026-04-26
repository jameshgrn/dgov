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

import ast
import logging
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dgov.event_types import (
    DgovEvent,
    IntegrationCandidateFailed,
    IntegrationCandidatePassed,
    IntegrationOverlapDetected,
    IntegrationRiskScored,
    SemanticGateRejected,
)

logger = logging.getLogger(__name__)

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
    emit_fn: Callable[[str, DgovEvent], None],
    session_root: str,
    plan_name: str,
    record: IntegrationRiskRecord,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_risk_scored event with structured payload."""
    event = IntegrationRiskScored(
        pane=pane,
        plan_name=plan_name,
        task_slug=record.task_slug,
        target_head_sha=record.target_head_sha,
        task_base_sha=record.task_base_sha,
        task_commit_sha=record.task_commit_sha,
        risk_level=record.risk_level.value,
        claimed_files=record.claimed_files,
        changed_files=record.changed_files,
        python_overlap_detected=record.python_overlap_detected,
        overlap_evidence=tuple(_serialize_evidence(e) for e in record.overlap_evidence),
    )
    emit_fn(session_root, event)


def emit_integration_overlap_detected(
    emit_fn: Callable[[str, DgovEvent], None],
    session_root: str,
    plan_name: str,
    task_slug: str,
    evidence: OverlapEvidence,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_overlap_detected event with evidence payload."""
    event = IntegrationOverlapDetected(
        pane=pane,
        plan_name=plan_name,
        task_slug=task_slug,
        evidence=_serialize_evidence(evidence),
    )
    emit_fn(session_root, event)


def emit_integration_candidate_passed(
    emit_fn: Callable[[str, DgovEvent], None],
    session_root: str,
    plan_name: str,
    verdict: IntegrationCandidateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_candidate_passed event with verdict payload."""
    event = IntegrationCandidatePassed(
        pane=pane,
        plan_name=plan_name,
        task_slug=verdict.task_slug,
        candidate_sha=verdict.candidate_sha,
        target_head_sha=verdict.target_head_sha,
        passed=verdict.passed,
        evidence=tuple(_serialize_evidence(e) for e in verdict.evidence),
    )
    emit_fn(session_root, event)


def emit_integration_candidate_failed(
    emit_fn: Callable[[str, DgovEvent], None],
    session_root: str,
    plan_name: str,
    verdict: IntegrationCandidateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit integration_candidate_failed event with verdict payload."""
    event = IntegrationCandidateFailed(
        pane=pane,
        plan_name=plan_name,
        task_slug=verdict.task_slug,
        candidate_sha=verdict.candidate_sha,
        target_head_sha=verdict.target_head_sha,
        passed=verdict.passed,
        failure_class=_fc_value(verdict.failure_class) or "",
        error_message=verdict.error_message or "",
        evidence=tuple(_serialize_evidence(e) for e in verdict.evidence),
    )
    emit_fn(session_root, event)


def emit_semantic_gate_rejected(
    emit_fn: Callable[[str, DgovEvent], None],
    session_root: str,
    plan_name: str,
    verdict: SemanticGateVerdict,
    pane: str = "semantic-settlement",
) -> None:
    """Emit semantic_gate_rejected event with gate verdict payload."""
    event = SemanticGateRejected(
        pane=pane,
        plan_name=plan_name,
        task_slug=verdict.task_slug,
        gate_name=verdict.gate_name,
        passed=verdict.passed,
        failure_class=_fc_value(verdict.failure_class) or "",
        error_message=verdict.error_message or "",
        evidence=tuple(_serialize_evidence(e) for e in verdict.evidence),
    )
    emit_fn(session_root, event)


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
# Python Semantic Analyzers
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SymbolInfo:
    """Internal representation of a Python symbol for analysis."""

    name: str
    symbol_type: str  # 'function', 'class', 'method', 'variable'
    file_path: str
    line_start: int
    line_end: int
    signature: str | None = None  # For functions/methods: "def name(arg: int) -> str"


def _extract_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract a clean signature string from a function definition node."""
    args = []
    for arg in node.args.posonlyargs:
        args.append(arg.arg)
    for arg in node.args.args:
        args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    for arg in node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    sig = f"def {node.name}({', '.join(args)})"
    if node.returns:
        sig += " -> ..."
    return sig


def _load_file_at_commit(project_root: str, commit_sha: str, rel_path: str) -> str | None:
    """Load file content from a specific git commit.

    Returns None if file cannot be retrieved.
    """
    result = subprocess.run(
        ["git", "show", f"{commit_sha}:{rel_path}"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.debug("Could not get %s at commit %s", rel_path, commit_sha)
        return None
    return result.stdout


def _parse_python_source(source: str, source_hint: str) -> ast.Module | None:
    """Parse Python source code into an AST.

    Returns None on syntax errors. source_hint is used for logging only.
    """
    try:
        return ast.parse(source)
    except SyntaxError:
        logger.debug("Syntax error in %s", source_hint)
        return None


def _is_method_node(node: ast.AST, class_nodes: list[ast.ClassDef]) -> bool:
    """Check if a function node is a method of any class in the given list."""
    return any(node in cls.body for cls in class_nodes)


def _extract_module_symbols(tree: ast.Module, file_path: str) -> dict[str, _SymbolInfo]:
    """Extract symbols from a parsed Python module AST.

    Returns a dict mapping symbol name to _SymbolInfo.
    """
    symbols: dict[str, _SymbolInfo] = {}

    # Collect all class definitions for method detection
    class_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip methods (handled via their class)
            if _is_method_node(node, class_nodes):
                continue

            sig = _extract_function_signature(node)
            symbols[node.name] = _SymbolInfo(
                name=node.name,
                symbol_type="function",
                file_path=file_path,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                signature=sig,
            )

        elif isinstance(node, ast.ClassDef):
            symbols[node.name] = _SymbolInfo(
                name=node.name,
                symbol_type="class",
                file_path=file_path,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                signature=None,
            )

            # Extract methods from class body
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig = _extract_function_signature(item)
                    method_name = f"{node.name}.{item.name}"
                    symbols[method_name] = _SymbolInfo(
                        name=method_name,
                        symbol_type="method",
                        file_path=file_path,
                        line_start=item.lineno,
                        line_end=getattr(item, "end_lineno", item.lineno),
                        signature=sig,
                    )

    return symbols


def _analyze_python_file_symbols(file_path: Path) -> dict[str, _SymbolInfo]:
    """Extract symbols from a Python file.

    Returns a dict mapping symbol name to SymbolInfo.
    Returns empty dict if file cannot be parsed.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        tree = _parse_python_source(content, str(file_path))
        if tree is None:
            return {}
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed to read %s: %s", file_path, exc)
        return {}

    return _extract_module_symbols(tree, str(file_path))


def _changed_symbols_since_base(
    base_symbols: dict[str, _SymbolInfo],
    derived_symbols: dict[str, _SymbolInfo],
) -> dict[str, _SymbolInfo]:
    changed: dict[str, _SymbolInfo] = {}
    for name, derived_sym in derived_symbols.items():
        base_sym = base_symbols.get(name)
        if base_sym is None:
            changed[name] = derived_sym
            continue
        if (
            base_sym.line_start != derived_sym.line_start
            or base_sym.line_end != derived_sym.line_end
            or base_sym.signature != derived_sym.signature
        ):
            changed[name] = derived_sym
    return changed


def _symbol_in_touched_files(symbol: _SymbolInfo, touched_files: set[str]) -> bool:
    rel_path = Path(symbol.file_path).name
    return rel_path in touched_files or symbol.file_path in touched_files


def _check_same_symbol_edit(
    task_base_symbols: dict[str, _SymbolInfo],
    task_commit_symbols: dict[str, _SymbolInfo],
    target_head_symbols: dict[str, _SymbolInfo],
    touched_files: set[str],
) -> list[SymbolOverlap]:
    """Detect when both task and target changed the same symbol."""
    overlaps: list[SymbolOverlap] = []
    task_changed = _changed_symbols_since_base(task_base_symbols, task_commit_symbols)
    target_changed = _changed_symbols_since_base(task_base_symbols, target_head_symbols)
    for name, task_sym in task_changed.items():
        if not _symbol_in_touched_files(task_sym, touched_files):
            continue
        target_sym = target_changed.get(name)
        if target_sym is None:
            continue
        overlaps.append(
            SymbolOverlap(
                symbol_name=name,
                symbol_type=task_sym.symbol_type,
                file_path=task_sym.file_path,
                task_line_range=(task_sym.line_start, task_sym.line_end),
                target_line_range=(target_sym.line_start, target_sym.line_end),
            )
        )

    return overlaps


def _is_test_file(file_path: str) -> bool:
    """Check if a file path represents a test file.

    Test files are identified by:
    - Being in a tests/ directory
    - Having 'test_' prefix in filename
    - Having '_test.py' suffix
    """
    path = Path(file_path)
    name = path.name
    # Check path components for 'tests' directory
    in_tests_dir = any(part == "tests" for part in path.parts)
    # Check filename patterns
    is_test_name = name.startswith("test_") or name.endswith("_test.py")
    return in_tests_dir or is_test_name


def _record_file_symbols(
    file_path: Path,
    all_symbols: dict[str, list[_SymbolInfo]],
    file_is_test: dict[str, bool],
) -> None:
    if file_path.suffix != ".py":
        return
    file_path_str = str(file_path)
    is_test = _is_test_file(file_path_str)
    file_is_test[file_path_str] = is_test
    file_is_test[str(file_path.resolve())] = is_test
    for name, info in _analyze_python_file_symbols(file_path).items():
        all_symbols.setdefault(name, []).append(info)


def _is_duplicate_test_only(
    infos: list[_SymbolInfo],
    file_is_test: dict[str, bool],
) -> bool:
    for info in infos:
        fp = info.file_path
        is_test = file_is_test.get(fp, False)
        if not is_test:
            try:
                is_test = file_is_test.get(str(Path(fp).resolve()), False)
            except (OSError, ValueError):
                is_test = False
        if not is_test and not _is_test_file(fp):
            return False
    return True


def _should_skip_duplicate_name(name: str) -> bool:
    return name.startswith("test_") or name in ("setup", "teardown", "fixture")


def _duplicate_definition_record(name: str, infos: list[_SymbolInfo]) -> DuplicateDefinition:
    return DuplicateDefinition(
        symbol_name=name,
        symbol_type=infos[0].symbol_type,
        file_paths=tuple(info.file_path for info in infos),
        line_numbers=tuple((info.line_start, info.line_end) for info in infos),
    )


def _check_duplicate_definitions(
    integrated_files: list[Path],
) -> list[DuplicateDefinition]:
    """Detect when the same symbol is defined multiple times across files."""
    all_symbols: dict[str, list[_SymbolInfo]] = {}
    file_is_test: dict[str, bool] = {}
    duplicates: list[DuplicateDefinition] = []

    for file_path in integrated_files:
        _record_file_symbols(file_path, all_symbols, file_is_test)

    for name, infos in all_symbols.items():
        if len(infos) <= 1:
            continue
        if _is_duplicate_test_only(infos, file_is_test):
            continue
        if _should_skip_duplicate_name(name):
            continue
        duplicates.append(_duplicate_definition_record(name, infos))

    return duplicates


def _check_signature_drift(
    base_symbols: dict[str, _SymbolInfo],
    integrated_symbols: dict[str, _SymbolInfo],
    touched_files: set[str],
) -> list[SignatureDrift]:
    """Detect when a public callable's signature changed.

    Only checks symbols in touched files.
    """
    drifts: list[SignatureDrift] = []

    for name, int_sym in integrated_symbols.items():
        # Only check functions/methods in touched files
        if int_sym.symbol_type not in ("function", "method"):
            continue

        rel_path = Path(int_sym.file_path).name
        if rel_path not in touched_files and int_sym.file_path not in touched_files:
            continue

        if name in base_symbols:
            base_sym = base_symbols[name]
            if base_sym.signature != int_sym.signature:
                drifts.append(
                    SignatureDrift(
                        symbol_name=name,
                        file_path=int_sym.file_path,
                        base_signature=base_sym.signature or "unknown",
                        integrated_signature=int_sym.signature or "unknown",
                    )
                )

    return drifts


def _get_symbols_at_commit(
    project_root: str,
    commit_sha: str,
    file_paths: list[str],
) -> dict[str, _SymbolInfo]:
    """Extract symbols from files at a specific git commit.

    Returns combined symbols from all files.
    """
    all_symbols: dict[str, _SymbolInfo] = {}

    for rel_path in file_paths:
        if not rel_path.endswith(".py"):
            continue

        content = _load_file_at_commit(project_root, commit_sha, rel_path)
        if content is None:
            continue

        source_hint = f"{rel_path} at commit {commit_sha}"
        tree = _parse_python_source(content, source_hint)
        if tree is None:
            continue

        file_symbols = _extract_module_symbols(tree, rel_path)
        all_symbols.update(file_symbols)

    return all_symbols


def _filter_python_files(touched_files: tuple[str, ...]) -> list[str]:
    """Filter touched files to only Python files."""
    return [f for f in touched_files if f.endswith(".py")]


def _validate_candidate_syntax(
    candidate_path: Path, py_files: list[str]
) -> tuple[list[Path], SyntaxConflict | None]:
    """Validate syntax of candidate Python files.

    Returns tuple of (valid_candidate_files, syntax_conflict).
    If syntax_conflict is not None, validation failed.
    """
    candidate_files: list[Path] = []
    for rel_path in py_files:
        full_path = candidate_path / rel_path
        if not full_path.exists():
            continue
        candidate_files.append(full_path)
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(content)
        except SyntaxError as exc:
            conflict = SyntaxConflict(
                file_path=str(rel_path),
                line_number=exc.lineno,
                column=exc.offset,
                error_message=str(exc),
                parser_used="python",
            )
            return ([], conflict)
    return (candidate_files, None)


def _extract_candidate_symbols(candidate_files: list[Path]) -> dict[str, _SymbolInfo]:
    """Extract symbols from all candidate Python files."""
    candidate_symbols: dict[str, _SymbolInfo] = {}
    for file_path in candidate_files:
        symbols = _analyze_python_file_symbols(file_path)
        candidate_symbols.update(symbols)
    return candidate_symbols


def _load_comparison_symbols(
    project_root: str,
    task_base_sha: str,
    task_commit_sha: str | None,
    target_head_sha: str,
    py_files: list[str],
    candidate_symbols: dict[str, _SymbolInfo],
) -> tuple[dict[str, _SymbolInfo], dict[str, _SymbolInfo], dict[str, _SymbolInfo]] | None:
    """Load symbols from git commits for comparison.

    Returns (task_base_symbols, task_commit_symbols, target_head_symbols) or None on error.
    """
    try:
        task_base_symbols = _get_symbols_at_commit(project_root, task_base_sha, py_files)
        target_head_symbols = _get_symbols_at_commit(project_root, target_head_sha, py_files)
        if task_commit_sha:
            task_commit_symbols = _get_symbols_at_commit(project_root, task_commit_sha, py_files)
        else:
            task_commit_symbols = candidate_symbols
        return (task_base_symbols, task_commit_symbols, target_head_symbols)
    except Exception as exc:
        logger.warning("Failed to get symbols for comparison: %s", exc)
        return None


def _check_signature_drifts(
    task_base_symbols: dict[str, _SymbolInfo],
    target_head_symbols: dict[str, _SymbolInfo],
    candidate_symbols: dict[str, _SymbolInfo],
    touched_set: set[str],
) -> list[SignatureDrift]:
    """Check for signature drift against both base and target."""
    drifts_from_base = _check_signature_drift(task_base_symbols, candidate_symbols, touched_set)
    drifts_from_target = _check_signature_drift(
        target_head_symbols, candidate_symbols, touched_set
    )

    # Combine unique drifts
    all_drifts: dict[str, SignatureDrift] = {}
    for d in drifts_from_base:
        all_drifts[d.symbol_name] = d
    for d in drifts_from_target:
        if d.symbol_name not in all_drifts:
            all_drifts[d.symbol_name] = d

    return list(all_drifts.values())


def _semantic_gate_pass(task_slug: str, gate_name: str = "python_semantic") -> SemanticGateVerdict:
    return SemanticGateVerdict(
        task_slug=task_slug,
        gate_name=gate_name,
        passed=True,
        checked_at=0.0,
    )


def _semantic_gate_fail(
    *,
    task_slug: str,
    gate_name: str,
    failure_class: FailureClass,
    evidence: tuple[OverlapEvidence, ...],
    error_message: str,
) -> SemanticGateVerdict:
    return SemanticGateVerdict(
        task_slug=task_slug,
        gate_name=gate_name,
        passed=False,
        failure_class=failure_class,
        evidence=evidence,
        error_message=error_message,
        checked_at=0.0,
    )


def _syntax_conflict_verdict(
    task_slug: str,
    syntax_conflict: SyntaxConflict,
) -> SemanticGateVerdict:
    return _semantic_gate_fail(
        task_slug=task_slug,
        gate_name="syntax_check",
        failure_class=FailureClass.SYNTAX_CONFLICT,
        evidence=(syntax_conflict,),
        error_message=(
            f"Syntax error in {syntax_conflict.file_path}: {syntax_conflict.error_message}"
        ),
    )


def _duplicate_definition_verdict(
    task_slug: str,
    duplicates: list[DuplicateDefinition],
) -> SemanticGateVerdict:
    return _semantic_gate_fail(
        task_slug=task_slug,
        gate_name="duplicate_definition",
        failure_class=FailureClass.DUPLICATE_DEFINITION,
        evidence=tuple(duplicates),
        error_message=f"Duplicate definitions found: {[item.symbol_name for item in duplicates]}",
    )


def _same_symbol_edit_verdict(
    task_slug: str,
    same_symbol_edits: list[SymbolOverlap],
) -> SemanticGateVerdict:
    return _semantic_gate_fail(
        task_slug=task_slug,
        gate_name="same_symbol_edit",
        failure_class=FailureClass.SAME_SYMBOL_EDIT,
        evidence=tuple(same_symbol_edits),
        error_message=f"Concurrent edits: {[item.symbol_name for item in same_symbol_edits]}",
    )


def _signature_drift_verdict(
    task_slug: str,
    drifts: list[SignatureDrift],
) -> SemanticGateVerdict:
    return _semantic_gate_fail(
        task_slug=task_slug,
        gate_name="signature_drift",
        failure_class=FailureClass.SIGNATURE_DRIFT,
        evidence=tuple(drifts),
        error_message=f"Signature drift detected: {[item.symbol_name for item in drifts]}",
    )


def _load_candidate_python_files(
    candidate_path: Path,
    touched_files: tuple[str, ...],
    task_slug: str,
) -> tuple[list[Path] | None, SemanticGateVerdict | None]:
    py_files = _filter_python_files(touched_files)
    if not py_files:
        return None, _semantic_gate_pass(task_slug)

    candidate_files, syntax_conflict = _validate_candidate_syntax(candidate_path, py_files)
    if syntax_conflict:
        return None, _syntax_conflict_verdict(task_slug, syntax_conflict)
    if not candidate_files:
        return None, _semantic_gate_pass(task_slug)
    return candidate_files, None


def _concurrent_symbol_verdict(
    *,
    task_slug: str,
    task_base_symbols: dict[str, _SymbolInfo],
    task_commit_symbols: dict[str, _SymbolInfo],
    target_head_symbols: dict[str, _SymbolInfo],
    candidate_symbols: dict[str, _SymbolInfo],
    touched_set: set[str],
) -> SemanticGateVerdict | None:
    same_symbol_edits = _check_same_symbol_edit(
        task_base_symbols, task_commit_symbols, target_head_symbols, touched_set
    )
    if same_symbol_edits:
        return _same_symbol_edit_verdict(task_slug, same_symbol_edits)

    drifts = _check_signature_drifts(
        task_base_symbols, target_head_symbols, candidate_symbols, touched_set
    )
    if drifts:
        return _signature_drift_verdict(task_slug, drifts)
    return None


def run_python_semantic_gate(
    candidate_path: Path,
    project_root: str,
    task_base_sha: str,
    task_commit_sha: str | None,
    target_head_sha: str,
    touched_files: tuple[str, ...],
    task_slug: str,
) -> SemanticGateVerdict:
    """Run deterministic Python semantic checks on an integration candidate."""
    candidate_files, early_verdict = _load_candidate_python_files(
        candidate_path,
        touched_files,
        task_slug,
    )
    if early_verdict is not None:
        return early_verdict

    py_files = _filter_python_files(touched_files)
    assert candidate_files is not None

    candidate_symbols = _extract_candidate_symbols(candidate_files)
    duplicates = _check_duplicate_definitions(candidate_files)
    if duplicates:
        return _duplicate_definition_verdict(task_slug, duplicates)

    comparison = _load_comparison_symbols(
        project_root, task_base_sha, task_commit_sha, target_head_sha, py_files, candidate_symbols
    )
    if comparison is None:
        return _semantic_gate_pass(task_slug)
    task_base_symbols, task_commit_symbols, target_head_symbols = comparison

    verdict = _concurrent_symbol_verdict(
        task_slug=task_slug,
        task_base_symbols=task_base_symbols,
        task_commit_symbols=task_commit_symbols,
        target_head_symbols=target_head_symbols,
        candidate_symbols=candidate_symbols,
        touched_set=set(touched_files),
    )
    if verdict is not None:
        return verdict

    return _semantic_gate_pass(task_slug)


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
    "run_python_semantic_gate",
]
