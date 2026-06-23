"""Helpers for dgov tests."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from dgov.cli import cli
from dgov.deploy_log import append as deploy_append
from dgov.event_types import (
    SettlementPhaseCompleted,
    SettlementPhaseStarted,
    deserialize_event,
    serialize_event,
)
from dgov.plan_review import (
    DiffStat,
    PlanReview,
    RunEnvelope,
    SettlementPhaseTiming,
    SettlementResult,
    UnitReview,
)
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
    _analyze_python_file_symbols,
    _check_duplicate_definitions,
    _check_same_symbol_edit,
    _check_signature_drift,
    _deserialize_evidence,
    _get_symbols_at_commit,
    _serialize_evidence,
    _SymbolInfo,
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
    run_python_semantic_gate,
    summarize_evidence,
)

__all__ = [
    "DiffStat",
    "DuplicateDefinition",
    "FailureClass",
    "IntegrationCandidateVerdict",
    "IntegrationRiskRecord",
    "OverlapEvidence",
    "PlanReview",
    "RiskLevel",
    "RunEnvelope",
    "SemanticGateVerdict",
    "SettlementPhaseCompleted",
    "SettlementPhaseStarted",
    "SettlementPhaseTiming",
    "SettlementResult",
    "SignatureDrift",
    "SymbolOverlap",
    "SyntaxConflict",
    "TextConflict",
    "UnitReview",
    "_SymbolInfo",
    "_analyze_python_file_symbols",
    "_check_duplicate_definitions",
    "_check_same_symbol_edit",
    "_check_signature_drift",
    "_deserialize_evidence",
    "_get_symbols_at_commit",
    "_serialize_evidence",
    "cli",
    "compile_plan_tree",
    "deploy_append",
    "describe_evidence",
    "describe_evidence_payload",
    "deserialize_event",
    "emit_integration_candidate_failed",
    "emit_integration_candidate_passed",
    "emit_integration_overlap_detected",
    "emit_integration_risk_scored",
    "emit_semantic_gate_rejected",
    "evidence_payload",
    "parse_evidence_payload",
    "parse_integration_candidate_verdict",
    "parse_integration_risk_record",
    "parse_semantic_gate_verdict",
    "run_python_semantic_gate",
    "serialize_event",
    "summarize_evidence",
    "write_test_provider_config",
]


def write_test_provider_config(root: Path) -> None:
    """Write a neutral provider config for tests that compile executable plans."""
    dgov_dir = root / ".dgov"
    dgov_dir.mkdir(exist_ok=True)
    (dgov_dir / "project.toml").write_text(
        """
[project]
provider = "test-provider"

[providers.test-provider]
default_agent = "provider/model-name"
base_url = "https://provider.example.com/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
""".lstrip()
    )


def compile_plan_tree(tmp_path: Path, name: str, tasks_toml: str, sections: str = "tasks") -> Path:
    """Create a Plan Tree structure and compile it using dgov compile.

    Returns the path to the compiled _compiled.toml.
    """
    runner = CliRunner()
    # 1. init-plan
    result = runner.invoke(
        cli, ["init-plan", name, "--sections", sections, "--force"], env={"DGOV_JSON": "1"}
    )
    if result.exit_code != 0:
        raise RuntimeError(f"init-plan failed: {result.output}")

    write_test_provider_config(Path.cwd())

    plan_root = Path(".dgov") / "plans" / name

    # 2. Write the task TOML (overwrite the example)
    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    first_section = section_list[0]
    task_file = plan_root / first_section / "main.toml"
    task_file.write_text(tasks_toml)

    # Remove example to avoid clutter
    for section in section_list:
        example = plan_root / section / "_example.toml"
        if example.exists():
            example.unlink()

    # 3. compile
    result = runner.invoke(cli, ["compile", str(plan_root)], env={"DGOV_JSON": "1"})
    if result.exit_code != 0:
        raise RuntimeError(f"compile failed: {result.output}")

    compiled_path = plan_root / "_compiled.toml"
    if not compiled_path.exists():
        raise RuntimeError(f"compile did not produce _compiled.toml at {compiled_path}")

    return compiled_path
