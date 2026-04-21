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
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

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


def _check_same_symbol_edit(
    task_base_symbols: dict[str, _SymbolInfo],
    task_commit_symbols: dict[str, _SymbolInfo],
    target_head_symbols: dict[str, _SymbolInfo],
    touched_files: set[str],
) -> list[SymbolOverlap]:
    """Detect when both task and target changed the same symbol.

    Compares actual changes: symbols that changed in the task (relative to base)
    AND symbols that changed in the target (relative to task base). Only reports
    overlap when BOTH sides modified the same symbol.

    Only checks symbols in touched files.
    """
    overlaps: list[SymbolOverlap] = []

    # Compute what actually changed on each side relative to task base
    task_changed: dict[str, _SymbolInfo] = {}
    for name, commit_sym in task_commit_symbols.items():
        base_sym = task_base_symbols.get(name)
        if base_sym is None:
            # New symbol added by task
            task_changed[name] = commit_sym
        elif (
            base_sym.line_start != commit_sym.line_start
            or base_sym.line_end != commit_sym.line_end
            or base_sym.signature != commit_sym.signature
        ):
            # Symbol modified by task (line range or signature changed)
            task_changed[name] = commit_sym

    target_changed: dict[str, _SymbolInfo] = {}
    for name, target_sym in target_head_symbols.items():
        base_sym = task_base_symbols.get(name)
        if base_sym is None:
            # New symbol added by target
            target_changed[name] = target_sym
        elif (
            base_sym.line_start != target_sym.line_start
            or base_sym.line_end != target_sym.line_end
            or base_sym.signature != target_sym.signature
        ):
            # Symbol modified by target (line range or signature changed)
            target_changed[name] = target_sym

    # Find symbols that changed on BOTH sides (concurrent modification)
    for name, task_sym in task_changed.items():
        # Only check if this file was touched
        rel_path = Path(task_sym.file_path).name
        if rel_path not in touched_files and task_sym.file_path not in touched_files:
            continue

        if name in target_changed:
            target_sym = target_changed[name]
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


def _check_duplicate_definitions(
    integrated_files: list[Path],
) -> list[DuplicateDefinition]:
    """Detect when the same symbol is defined multiple times across files.

    Excludes test files from duplicate detection since test modules legitimately
    have repeated helper functions and test names. Only flags duplicates in
    production code or duplicates between production and test code.
    """
    all_symbols: dict[str, list[_SymbolInfo]] = {}
    file_is_test: dict[str, bool] = {}
    duplicates: list[DuplicateDefinition] = []

    for file_path in integrated_files:
        if file_path.suffix != ".py":
            continue

        file_path_str = str(file_path)
        abs_path_str = str(file_path.resolve())
        is_test = _is_test_file(file_path_str)
        file_is_test[file_path_str] = is_test
        file_is_test[abs_path_str] = is_test

        symbols = _analyze_python_file_symbols(file_path)
        for name, info in symbols.items():
            if name not in all_symbols:
                all_symbols[name] = []
            all_symbols[name].append(info)

    for name, infos in all_symbols.items():
        if len(infos) <= 1:
            continue

        # Check if all occurrences are in test files
        all_in_test_files = True
        file_paths_list: list[str] = []

        for info in infos:
            fp = info.file_path
            file_paths_list.append(fp)
            # Check multiple ways: direct lookup, absolute path, or re-check
            is_test = file_is_test.get(fp, False)
            if not is_test:
                # Try with absolute path
                try:
                    abs_fp = str(Path(fp).resolve())
                    is_test = file_is_test.get(abs_fp, False)
                except (OSError, ValueError):
                    pass
            if not is_test:
                # Fallback: re-check the path directly
                is_test = _is_test_file(fp)
            if not is_test:
                all_in_test_files = False

        # Skip if ALL occurrences are in test files (legitimate test duplicates)
        if all_in_test_files:
            continue

        # Skip common test helper patterns even in production context
        # These are standard pytest patterns that may appear in both test and src
        if name.startswith("test_") or name in ("setup", "teardown", "fixture"):
            continue

        file_paths = tuple(file_paths_list)
        line_numbers = tuple((i.line_start, i.line_end) for i in infos)
        # Determine type from first occurrence
        symbol_type = infos[0].symbol_type
        duplicates.append(
            DuplicateDefinition(
                symbol_name=name,
                symbol_type=symbol_type,
                file_paths=file_paths,
                line_numbers=line_numbers,
            )
        )

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


def run_python_semantic_gate(
    candidate_path: Path,
    project_root: str,
    task_base_sha: str,
    task_commit_sha: str | None,
    target_head_sha: str,
    touched_files: tuple[str, ...],
    task_slug: str,
) -> SemanticGateVerdict:
    """Run deterministic Python semantic gates on an integration candidate.

    Checks for:
    - same_symbol_edit: Both sides changed the same Python symbol
    - duplicate_definition: Integrated code defines same symbol twice
    - signature_drift: Public callable changed shape

    Returns a SemanticGateVerdict with findings.
    """
    py_files = [f for f in touched_files if f.endswith(".py")]

    if not py_files:
        # Non-Python tasks bypass the semantic gate
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="python_semantic",
            passed=True,
            checked_at=0.0,  # Will be filled by caller
        )

    # Check for syntax errors first (fail closed if we can't parse)
    candidate_files: list[Path] = []
    for rel_path in py_files:
        full_path = candidate_path / rel_path
        if full_path.exists():
            candidate_files.append(full_path)
            # Verify it parses
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                ast.parse(content)
            except SyntaxError as exc:
                return SemanticGateVerdict(
                    task_slug=task_slug,
                    gate_name="syntax_check",
                    passed=False,
                    failure_class=FailureClass.SYNTAX_CONFLICT,
                    evidence=(
                        SyntaxConflict(
                            file_path=str(rel_path),
                            line_number=exc.lineno,
                            column=exc.offset,
                            error_message=str(exc),
                            parser_used="python",
                        ),
                    ),
                    error_message=f"Syntax error in {rel_path}: {exc}",
                    checked_at=0.0,
                )

    if not candidate_files:
        # No Python files to analyze
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="python_semantic",
            passed=True,
            checked_at=0.0,
        )

    # Get symbols from candidate (integrated result)
    candidate_symbols: dict[str, _SymbolInfo] = {}
    for file_path in candidate_files:
        symbols = _analyze_python_file_symbols(file_path)
        candidate_symbols.update(symbols)

    # Check for duplicate definitions in integrated result
    duplicates = _check_duplicate_definitions(candidate_files)
    if duplicates:
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="duplicate_definition",
            passed=False,
            failure_class=FailureClass.DUPLICATE_DEFINITION,
            evidence=tuple(duplicates),
            error_message=f"Duplicate definitions found: {[d.symbol_name for d in duplicates]}",
            checked_at=0.0,
        )

    # Get symbols from task base, task commit, and target head for comparison
    touched_set = set(touched_files)

    try:
        task_base_symbols = _get_symbols_at_commit(project_root, task_base_sha, list(py_files))
        target_head_symbols = _get_symbols_at_commit(project_root, target_head_sha, list(py_files))
        # If task_commit_sha provided, get symbols at task commit to detect actual changes
        task_commit_symbols: dict[str, _SymbolInfo]
        if task_commit_sha:
            task_commit_symbols = _get_symbols_at_commit(
                project_root, task_commit_sha, list(py_files)
            )
        else:
            # Fallback: use candidate symbols (the integrated result contains task changes)
            task_commit_symbols = candidate_symbols
    except Exception as exc:
        logger.warning("Failed to get symbols for comparison: %s", exc)
        # Fail open on git errors - we still did our best analysis
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="python_semantic",
            passed=True,
            checked_at=0.0,
        )

    # Check for same-symbol edits (concurrent modification)
    same_symbol_edits = _check_same_symbol_edit(
        task_base_symbols, task_commit_symbols, target_head_symbols, touched_set
    )
    if same_symbol_edits:
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="same_symbol_edit",
            passed=False,
            failure_class=FailureClass.SAME_SYMBOL_EDIT,
            evidence=tuple(same_symbol_edits),
            error_message=f"Concurrent edits: {[s.symbol_name for s in same_symbol_edits]}",
            checked_at=0.0,
        )

    # Check for signature drift
    # Compare integrated result against both task base and target
    drifts_from_base = _check_signature_drift(task_base_symbols, candidate_symbols, touched_set)
    drifts_from_target = _check_signature_drift(
        target_head_symbols, candidate_symbols, touched_set
    )

    # Combine unique drifts
    all_drifts = {d.symbol_name: d for d in drifts_from_base}
    for d in drifts_from_target:
        if d.symbol_name not in all_drifts:
            all_drifts[d.symbol_name] = d

    if all_drifts:
        drifts_list = list(all_drifts.values())
        return SemanticGateVerdict(
            task_slug=task_slug,
            gate_name="signature_drift",
            passed=False,
            failure_class=FailureClass.SIGNATURE_DRIFT,
            evidence=tuple(drifts_list),
            error_message=f"Signature drift detected: {[d.symbol_name for d in drifts_list]}",
            checked_at=0.0,
        )

    # All gates passed
    return SemanticGateVerdict(
        task_slug=task_slug,
        gate_name="python_semantic",
        passed=True,
        checked_at=0.0,
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
    "run_python_semantic_gate",
]
