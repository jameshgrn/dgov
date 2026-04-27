"""Settlement flow orchestration for integration-aware landing.

This module owns the candidate lifecycle and semantic-settlement execution
that used to live directly in ``EventDagRunner``. The runner remains the
governor-facing coordinator; this module owns the settlement algorithm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from dgov import deploy_log
from dgov.actions import MergeTask
from dgov.config import ProjectConfig
from dgov.dag_parser import DagTaskSpec
from dgov.event_types import DgovEvent, ReviewerVerdict
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
    _check_duplicate_definitions,
    _check_same_symbol_edit,
    _check_signature_drift,
    _get_symbols_at_commit,
    emit_integration_candidate_failed,
    emit_integration_candidate_passed,
    emit_integration_overlap_detected,
    emit_integration_risk_scored,
    emit_semantic_gate_rejected,
    parse_semantic_gate_verdict,
)
from dgov.settlement import autofix_sandbox, validate_sandbox
from dgov.types import Worktree
from dgov.worktree import (
    IntegrationCandidateResult,
    commit_in_worktree,
    create_integration_candidate,
    merge_worktree,
    remove_integration_candidate,
)

logger = logging.getLogger(__name__)

_SEMANTIC_GATE_SUBPROCESS = """
import json
import sys
from pathlib import Path

from dgov.semantic_settlement import _evidence_payload, run_python_semantic_gate

payload = json.loads(sys.argv[1])
verdict = run_python_semantic_gate(
    candidate_path=Path(payload["candidate_path"]),
    project_root=payload["project_root"],
    task_base_sha=payload["task_base_sha"],
    task_commit_sha=payload["task_commit_sha"],
    target_head_sha=payload["target_head_sha"],
    touched_files=tuple(payload["touched_files"]),
    task_slug=payload["task_slug"],
)
print(
    json.dumps(
        {
            "task_slug": verdict.task_slug,
            "gate_name": verdict.gate_name,
            "passed": verdict.passed,
            "failure_class": verdict.failure_class.value if verdict.failure_class else None,
            "error_message": verdict.error_message,
            "evidence": _evidence_payload(verdict.evidence),
        }
    )
)
"""


def _semantic_gate_payload(
    *,
    candidate_path: Path,
    project_root: str,
    task_base_sha: str,
    task_commit_sha: str | None,
    target_head_sha: str,
    touched_files: tuple[str, ...],
    task_slug: str,
) -> dict[str, object]:
    return {
        "candidate_path": str(candidate_path),
        "project_root": project_root,
        "task_base_sha": task_base_sha,
        "task_commit_sha": task_commit_sha,
        "target_head_sha": target_head_sha,
        "touched_files": list(touched_files),
        "task_slug": task_slug,
    }


def _semantic_gate_env(candidate_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    candidate_src = candidate_path / "src"
    pythonpath_root = candidate_src if candidate_src.exists() else candidate_path
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(pythonpath_root)
        if not existing_pythonpath
        else f"{pythonpath_root}{os.pathsep}{existing_pythonpath}"
    )
    return env


def _semantic_gate_failure(task_slug: str, message: str) -> SemanticGateVerdict:
    return SemanticGateVerdict(
        task_slug=task_slug,
        gate_name="python_semantic_subprocess",
        passed=False,
        error_message=message,
        checked_at=0.0,
    )


def run_python_semantic_gate_in_subprocess(
    *,
    candidate_path: Path,
    project_root: str,
    task_base_sha: str,
    task_commit_sha: str | None,
    target_head_sha: str,
    touched_files: tuple[str, ...],
    task_slug: str,
) -> SemanticGateVerdict:
    """Run the semantic gate from the candidate snapshot, not the live governor process."""
    payload = _semantic_gate_payload(
        candidate_path=candidate_path,
        project_root=project_root,
        task_base_sha=task_base_sha,
        task_commit_sha=task_commit_sha,
        target_head_sha=target_head_sha,
        touched_files=touched_files,
        task_slug=task_slug,
    )
    result = subprocess.run(
        [sys.executable, "-c", _SEMANTIC_GATE_SUBPROCESS, json.dumps(payload)],
        cwd=candidate_path,
        capture_output=True,
        text=True,
        env=_semantic_gate_env(candidate_path),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return _semantic_gate_failure(
            task_slug,
            f"Failed to execute semantic gate in candidate subprocess: {detail}",
        )
    try:
        return parse_semantic_gate_verdict(json.loads(result.stdout))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        detail = result.stdout.strip() or result.stderr.strip() or "empty output"
        return _semantic_gate_failure(
            task_slug,
            f"Failed to parse semantic gate verdict from candidate subprocess: {exc}: {detail}",
        )


class SettlementFlow:
    """Own the integration-candidate settlement algorithm for one runner context."""

    def __init__(
        self,
        *,
        session_root: str,
        plan_name: str,
        project_config: ProjectConfig,
    ) -> None:
        self.session_root = session_root
        self.plan_name = plan_name
        self.project_config = project_config

    def _task_config(self, task: DagTaskSpec) -> ProjectConfig:
        if not task.test_cmd:
            return self.project_config
        return replace(self.project_config, test_cmd=task.test_cmd)

    def _git_rev_parse(self, ref: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=self.session_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _changed_files_between(self, base_ref: str, head_ref: str) -> tuple[str, ...]:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, head_ref],
            cwd=self.session_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ()
        return tuple(path for path in result.stdout.strip().split("\n") if path)

    def _risk_level_from_evidence(
        self, overlap_evidence: tuple[OverlapEvidence, ...]
    ) -> RiskLevel:
        if not overlap_evidence:
            return RiskLevel.NONE

        evidence_types = {type(evidence) for evidence in overlap_evidence}
        if len(evidence_types) > 1 or len(overlap_evidence) > 3:
            return RiskLevel.CRITICAL
        if any(isinstance(evidence, DuplicateDefinition) for evidence in overlap_evidence):
            return RiskLevel.HIGH
        if any(isinstance(evidence, SymbolOverlap) for evidence in overlap_evidence):
            return RiskLevel.MEDIUM
        if all(isinstance(evidence, SignatureDrift) for evidence in overlap_evidence):
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    def _collect_overlap_evidence(
        self,
        *,
        wt: Worktree,
        target_head_sha: str,
        task_commit_sha: str,
        py_files: tuple[str, ...],
    ) -> tuple[OverlapEvidence, ...]:
        if not py_files or not target_head_sha or not task_commit_sha:
            return ()

        try:
            task_base_symbols = _get_symbols_at_commit(
                self.session_root, wt.commit, list(py_files)
            )
            task_commit_symbols = _get_symbols_at_commit(
                self.session_root, task_commit_sha, list(py_files)
            )
            target_head_symbols = _get_symbols_at_commit(
                self.session_root, target_head_sha, list(py_files)
            )
            touched_set = set(py_files)
            evidence: list[OverlapEvidence] = []
            evidence.extend(
                _check_same_symbol_edit(
                    task_base_symbols,
                    task_commit_symbols,
                    target_head_symbols,
                    touched_set,
                )
            )
            evidence.extend(
                _check_signature_drift(task_base_symbols, task_commit_symbols, touched_set)
            )
            evidence.extend(
                _check_duplicate_definitions([
                    wt.path / path
                    for path in py_files
                    if (wt.path / path).exists() and (wt.path / path).is_file()
                ])
            )
            return tuple(evidence)
        except Exception as exc:
            logger.warning("Failed to compute semantic overlap evidence: %s", exc)
            return ()

    def compute_semantic_risk(
        self,
        *,
        action: MergeTask,
        wt: Worktree,
        file_claims: tuple[str, ...],
    ) -> IntegrationRiskRecord:
        """Compute a telemetry-only integration risk record for the task commit."""
        target_head = self._git_rev_parse("HEAD") or ""
        changed_files = self._changed_files_between(wt.commit, wt.branch)
        task_commit_sha = self._get_task_commit_sha(wt) or wt.branch
        py_files = tuple(path for path in changed_files if path.endswith(".py"))
        overlap_evidence = self._collect_overlap_evidence(
            wt=wt,
            target_head_sha=target_head,
            task_commit_sha=task_commit_sha,
            py_files=py_files,
        )
        risk_level = self._risk_level_from_evidence(overlap_evidence)

        return IntegrationRiskRecord(
            task_slug=action.task_slug,
            target_head_sha=target_head,
            task_base_sha=wt.commit,
            task_commit_sha=task_commit_sha,
            risk_level=risk_level,
            claimed_files=file_claims,
            changed_files=changed_files,
            python_overlap_detected=bool(overlap_evidence),
            overlap_evidence=overlap_evidence,
            computed_at=time.time(),
        )

    def emit_risk_events(
        self,
        *,
        action: MergeTask,
        risk_record: IntegrationRiskRecord,
        emit_event_fn: Callable[[str, DgovEvent], None],
    ) -> None:
        """Emit semantic-settlement telemetry events for a task."""
        emit_integration_risk_scored(
            emit_event_fn,
            self.session_root,
            self.plan_name,
            risk_record,
        )
        for evidence in risk_record.overlap_evidence:
            emit_integration_overlap_detected(
                emit_event_fn,
                self.session_root,
                self.plan_name,
                action.task_slug,
                evidence,
            )

    async def _handle_read_only_role(
        self,
        *,
        task: DagTaskSpec,
        action: MergeTask,
        wt: Worktree,
        emit_event_fn: Callable[[str, DgovEvent], None],
        deploy_append_fn: Any,
    ) -> tuple[str | None, bool]:
        deploy_append_fn(self.session_root, self.plan_name, action.task_slug, wt.commit)
        if task.role == "reviewer":
            emit_event_fn(
                self.session_root,
                ReviewerVerdict(
                    pane=action.pane_slug,
                    plan_name=self.plan_name,
                    task_slug=action.task_slug,
                ),
            )
            logger.info("REVIEWED %s", action.task_slug)
        else:
            logger.info("RESEARCHED %s", action.task_slug)
        return None, False

    async def prepare_and_commit(
        self,
        *,
        task: DagTaskSpec,
        action: MergeTask,
        wt: Worktree,
        emit_event_fn: Callable[[str, DgovEvent], None],
        autofix_fn: Any = None,
        commit_fn: Any = None,
        deploy_append_fn: Any = None,
    ) -> tuple[str | None, bool]:
        """Autofix, commit, and handle read-only roles."""
        if autofix_fn is None:
            autofix_fn = autofix_sandbox
        if commit_fn is None:
            commit_fn = commit_in_worktree
        if deploy_append_fn is None:
            deploy_append_fn = deploy_log.append

        if task.role in ("researcher", "reviewer"):
            return await self._handle_read_only_role(
                task=task,
                action=action,
                wt=wt,
                emit_event_fn=emit_event_fn,
                deploy_append_fn=deploy_append_fn,
            )

        file_claims = action.file_claims
        task_config = self._task_config(task)
        await asyncio.to_thread(autofix_fn, wt.path, file_claims, task_config)
        msg = task.commit_message or f"feat: completed {action.task_slug}"
        await asyncio.to_thread(commit_fn, wt, msg, file_claims)
        return None, True

    async def run_isolated_validation(
        self,
        *,
        task: DagTaskSpec,
        action: MergeTask,
        wt: Worktree,
        emit_event_fn: Callable[[str, DgovEvent], None],
        validate_fn: Any = None,
    ) -> tuple[str | None, IntegrationRiskRecord | None]:
        """Compute integration risk and run isolated validation gates."""
        if validate_fn is None:
            validate_fn = validate_sandbox

        task_config = self._task_config(task)
        risk_record = self.compute_semantic_risk(
            action=action,
            wt=wt,
            file_claims=action.file_claims,
        )
        self.emit_risk_events(
            action=action,
            risk_record=risk_record,
            emit_event_fn=emit_event_fn,
        )

        gate_result = await asyncio.to_thread(
            validate_fn,
            wt.path,
            wt.commit,
            self.session_root,
            task_config,
        )
        if not gate_result.passed:
            return gate_result.error, risk_record
        return None, risk_record

    def _get_task_commit_sha(self, wt: Worktree) -> str | None:
        return self._git_rev_parse(wt.branch)

    async def _remove_candidate(
        self,
        *,
        candidate_path: Path | None,
        remove_candidate_fn: Any,
    ) -> None:
        if candidate_path is None:
            return
        await asyncio.to_thread(
            remove_candidate_fn,
            self.session_root,
            candidate_path,
        )

    def _failed_candidate_verdict(
        self,
        *,
        action: MergeTask,
        candidate_sha: str,
        error_message: str,
        failure_class: FailureClass,
    ) -> IntegrationCandidateVerdict:
        return IntegrationCandidateVerdict(
            task_slug=action.task_slug,
            candidate_sha=candidate_sha,
            target_head_sha="",
            passed=False,
            failure_class=failure_class,
            error_message=error_message,
            validated_at=time.time(),
        )

    def _passed_candidate_verdict(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
    ) -> IntegrationCandidateVerdict:
        return IntegrationCandidateVerdict(
            task_slug=action.task_slug,
            candidate_sha=candidate_result.candidate_sha or "",
            target_head_sha="",
            passed=True,
            validated_at=time.time(),
        )

    async def _reject_semantic_gate_candidate(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        semantic_verdict: SemanticGateVerdict,
        emit_event_fn: Callable[[str, DgovEvent], None],
        remove_candidate_fn: Any,
        rejected_emit_fn: Any,
    ) -> str:
        await self._remove_candidate(
            candidate_path=candidate_result.candidate_path,
            remove_candidate_fn=remove_candidate_fn,
        )
        verdict = SemanticGateVerdict(
            task_slug=semantic_verdict.task_slug,
            gate_name=semantic_verdict.gate_name,
            passed=False,
            failure_class=semantic_verdict.failure_class,
            evidence=semantic_verdict.evidence,
            error_message=semantic_verdict.error_message,
            checked_at=time.time(),
        )
        rejected_emit_fn(
            emit_event_fn,
            self.session_root,
            self.plan_name,
            verdict,
            pane=action.pane_slug,
        )
        return semantic_verdict.error_message or (
            f"Semantic gate '{semantic_verdict.gate_name}' rejected"
        )

    async def run_semantic_gate_on_candidate(
        self,
        *,
        action: MergeTask,
        wt: Worktree,
        candidate_result: IntegrationCandidateResult,
        risk_record: IntegrationRiskRecord,
        emit_event_fn: Callable[[str, DgovEvent], None],
        remove_candidate_fn: Any = None,
        semantic_gate_fn: Any = None,
        rejected_emit_fn: Any = None,
    ) -> str | None:
        """Run the deterministic Python semantic gate on the integrated candidate."""
        if remove_candidate_fn is None:
            remove_candidate_fn = remove_integration_candidate
        if semantic_gate_fn is None:
            semantic_gate_fn = run_python_semantic_gate_in_subprocess
        if rejected_emit_fn is None:
            rejected_emit_fn = emit_semantic_gate_rejected
        if candidate_result.candidate_path is None:
            return None

        semantic_verdict = semantic_gate_fn(
            candidate_path=candidate_result.candidate_path,
            project_root=self.session_root,
            task_base_sha=wt.commit,
            task_commit_sha=self._get_task_commit_sha(wt),
            target_head_sha=risk_record.target_head_sha,
            touched_files=action.file_claims,
            task_slug=action.task_slug,
        )
        if semantic_verdict.passed:
            return None

        return await self._reject_semantic_gate_candidate(
            action=action,
            candidate_result=candidate_result,
            semantic_verdict=semantic_verdict,
            emit_event_fn=emit_event_fn,
            remove_candidate_fn=remove_candidate_fn,
            rejected_emit_fn=rejected_emit_fn,
        )

    async def cleanup_rejected_candidate(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        verdict: IntegrationCandidateVerdict,
        emit_event_fn: Callable[[str, DgovEvent], None],
        remove_candidate_fn: Any = None,
        failed_emit_fn: Any = None,
    ) -> None:
        """Remove a rejected candidate and emit the failure event."""
        if remove_candidate_fn is None:
            remove_candidate_fn = remove_integration_candidate
        if failed_emit_fn is None:
            failed_emit_fn = emit_integration_candidate_failed
        await self._remove_candidate(
            candidate_path=candidate_result.candidate_path,
            remove_candidate_fn=remove_candidate_fn,
        )
        failed_emit_fn(
            emit_event_fn,
            self.session_root,
            self.plan_name,
            verdict,
            pane=action.pane_slug,
        )

    async def cleanup_passed_candidate(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        emit_event_fn: Callable[[str, DgovEvent], None],
        remove_candidate_fn: Any = None,
        passed_emit_fn: Any = None,
    ) -> None:
        """Remove a passed candidate and emit the success event."""
        if remove_candidate_fn is None:
            remove_candidate_fn = remove_integration_candidate
        if passed_emit_fn is None:
            passed_emit_fn = emit_integration_candidate_passed
        await self._remove_candidate(
            candidate_path=candidate_result.candidate_path,
            remove_candidate_fn=remove_candidate_fn,
        )
        passed_emit_fn(
            emit_event_fn,
            self.session_root,
            self.plan_name,
            self._passed_candidate_verdict(action=action, candidate_result=candidate_result),
            pane=action.pane_slug,
        )

    async def validate_and_finalize_candidate(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        task_config: ProjectConfig,
        emit_event_fn: Any,
        validate_fn: Any = None,
        remove_candidate_fn: Any = None,
        failed_emit_fn: Any = None,
        passed_emit_fn: Any = None,
    ) -> str | None:
        """Validate the integrated candidate with the same gates as isolated validation."""
        if validate_fn is None:
            validate_fn = validate_sandbox
        if failed_emit_fn is None:
            failed_emit_fn = emit_integration_candidate_failed
        if passed_emit_fn is None:
            passed_emit_fn = emit_integration_candidate_passed
        if candidate_result.candidate_path is None:
            return None

        gate_result = await asyncio.to_thread(
            validate_fn,
            candidate_result.candidate_path,
            candidate_result.candidate_sha,
            self.session_root,
            task_config,
        )
        if gate_result.passed:
            await self.cleanup_passed_candidate(
                action=action,
                candidate_result=candidate_result,
                emit_event_fn=emit_event_fn,
                remove_candidate_fn=remove_candidate_fn,
                passed_emit_fn=passed_emit_fn,
            )
            return None

        return await self._reject_failed_candidate_validation(
            action=action,
            candidate_result=candidate_result,
            gate_error=gate_result.error,
            emit_event_fn=emit_event_fn,
            remove_candidate_fn=remove_candidate_fn,
            failed_emit_fn=failed_emit_fn,
        )

    async def _reject_failed_candidate_validation(
        self,
        *,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        gate_error: str | None,
        emit_event_fn: Any,
        remove_candidate_fn: Any,
        failed_emit_fn: Any,
    ) -> str:
        verdict = self._failed_candidate_verdict(
            action=action,
            candidate_sha=candidate_result.candidate_sha,
            error_message=gate_error or "Integrated candidate failed validation gates",
            failure_class=FailureClass.BEHAVIORAL_MISMATCH,
        )
        await self.cleanup_rejected_candidate(
            action=action,
            candidate_result=candidate_result,
            verdict=verdict,
            emit_event_fn=emit_event_fn,
            remove_candidate_fn=remove_candidate_fn,
            failed_emit_fn=failed_emit_fn,
        )
        return gate_error or "Integrated candidate validation failed"

    async def create_integration_candidate_with_emit(
        self,
        *,
        action: MergeTask,
        wt: Worktree,
        emit_event_fn: Any,
        create_candidate_fn: Any = None,
        failed_emit_fn: Any = None,
    ) -> IntegrationCandidateResult:
        """Create the integration candidate and emit a failure event on replay failure."""
        if create_candidate_fn is None:
            create_candidate_fn = create_integration_candidate
        if failed_emit_fn is None:
            failed_emit_fn = emit_integration_candidate_failed
        candidate_slug = f"{action.task_slug}-candidate"
        candidate_result = await asyncio.to_thread(
            create_candidate_fn,
            self.session_root,
            wt,
            candidate_slug,
        )
        if candidate_result.passed:
            return candidate_result

        verdict = self._failed_candidate_verdict(
            action=action,
            candidate_sha="",
            error_message=candidate_result.error or "Failed to create integration candidate",
            failure_class=FailureClass.TEXT_CONFLICT,
        )
        failed_emit_fn(
            emit_event_fn,
            self.session_root,
            self.plan_name,
            verdict,
            pane=action.pane_slug,
        )
        return candidate_result

    async def finalize_merge(
        self,
        *,
        action: MergeTask,
        wt: Worktree,
        merge_fn: Any = None,
        deploy_append_fn: Any = None,
    ) -> None:
        """Merge the worktree and record the deploy log entry."""
        if merge_fn is None:
            merge_fn = merge_worktree
        if deploy_append_fn is None:
            deploy_append_fn = deploy_log.append
        merge_sha = await asyncio.to_thread(
            merge_fn,
            self.session_root,
            wt,
        )
        logger.info("COMMITTED %s", action.task_slug)
        deploy_append_fn(self.session_root, self.plan_name, action.task_slug, merge_sha)


__all__ = [
    "FailureClass",
    "IntegrationCandidateResult",
    "IntegrationCandidateVerdict",
    "IntegrationRiskRecord",
    "RiskLevel",
    "SemanticGateVerdict",
    "SettlementFlow",
    "SymbolOverlap",
    "autofix_sandbox",
    "commit_in_worktree",
    "create_integration_candidate",
    "emit_integration_candidate_failed",
    "emit_integration_candidate_passed",
    "emit_integration_overlap_detected",
    "emit_integration_risk_scored",
    "emit_semantic_gate_rejected",
    "merge_worktree",
    "remove_integration_candidate",
    "run_python_semantic_gate_in_subprocess",
    "validate_sandbox",
]
