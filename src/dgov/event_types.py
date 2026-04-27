"""Typed event dataclasses for dgov event system.

This module defines frozen dataclasses for all actively emitted event types,
providing type safety and serialization/deserialization logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal


@dataclass(frozen=True)
class RunStart:
    """Emitted at the beginning of a DAG run."""

    event_type: Literal["run_start"] = "run_start"
    pane: str = ""
    plan_name: str = ""


@dataclass(frozen=True)
class RunCompleted:
    """Emitted at the end of a DAG run with final status."""

    event_type: Literal["run_completed"] = "run_completed"
    pane: str = ""
    plan_name: str = ""
    run_status: str = ""
    duration_s: float = 0.0
    sentrux: str = ""  # JSON string


@dataclass(frozen=True)
class EvtTaskDispatched:
    """Emitted when a task is dispatched to a worker."""

    event_type: Literal["dag_task_dispatched"] = "dag_task_dispatched"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    agent: str = ""


@dataclass(frozen=True)
class TaskDone:
    """Emitted when a task completes successfully."""

    event_type: Literal["task_done"] = "task_done"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str | None = None
    duration: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class TaskFailed:
    """Emitted when a task fails."""

    event_type: Literal["task_failed"] = "task_failed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str = ""
    duration: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class TaskAbandoned:
    """Emitted when a task is abandoned (e.g., during shutdown)."""

    event_type: Literal["task_abandoned"] = "task_abandoned"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ReviewPass:
    """Emitted when structural review passes."""

    event_type: Literal["review_pass"] = "review_pass"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    verdict: str = ""


@dataclass(frozen=True)
class ReviewFail:
    """Emitted when structural review fails."""

    event_type: Literal["review_fail"] = "review_fail"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    verdict: str = ""
    error: str = ""


@dataclass(frozen=True)
class ReviewerVerdict:
    """Emitted when a reviewer role task completes."""

    event_type: Literal["reviewer_verdict"] = "reviewer_verdict"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""


@dataclass(frozen=True)
class MergeCompleted:
    """Emitted when a task is successfully merged."""

    event_type: Literal["merge_completed"] = "merge_completed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str | None = None


@dataclass(frozen=True)
class TaskMergeFailed:
    """Emitted when a task merge fails."""

    event_type: Literal["task_merge_failed"] = "task_merge_failed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str = ""


@dataclass(frozen=True)
class SettlementRetry:
    """Emitted when settlement retries a task after rejection."""

    event_type: Literal["settlement_retry"] = "settlement_retry"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str = ""


@dataclass(frozen=True)
class SelfReviewPassed:
    """Emitted when self-review passes."""

    event_type: Literal["self_review_passed"] = "self_review_passed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""


@dataclass(frozen=True)
class SelfReviewRejected:
    """Emitted when self-review finds issues."""

    event_type: Literal["self_review_rejected"] = "self_review_rejected"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    findings: str = ""


@dataclass(frozen=True)
class SelfReviewAutoPassed:
    """Emitted when self-review auto-passes on second attempt."""

    event_type: Literal["self_review_auto_passed"] = "self_review_auto_passed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    findings: str | None = None


@dataclass(frozen=True)
class SelfReviewFixStarted:
    """Emitted when worker is relaunched to fix self-review findings."""

    event_type: Literal["self_review_fix_started"] = "self_review_fix_started"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""


@dataclass(frozen=True)
class SelfReviewError:
    """Emitted when self-review encounters an error."""

    event_type: Literal["self_review_error"] = "self_review_error"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    error: str = ""


@dataclass(frozen=True)
class IterationFork:
    """Emitted when a task forks due to iteration exhaustion."""

    event_type: Literal["iteration_fork"] = "iteration_fork"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    fork_depth: int = 0


@dataclass(frozen=True)
class GovernorResumed:
    """Emitted when the governor resumes a failed task."""

    event_type: Literal["dag_task_governor_resumed"] = "dag_task_governor_resumed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    action: str = ""  # GovernorAction value


@dataclass(frozen=True)
class ShutdownRequested:
    """Emitted when shutdown is requested via signal."""

    event_type: Literal["shutdown_requested"] = "shutdown_requested"
    pane: str = ""
    plan_name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class WorkerLog:
    """Emitted for worker log events."""

    event_type: Literal["worker_log"] = "worker_log"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    log_type: str = ""  # e.g., "call", "error", "done"
    content: Any = None


@dataclass(frozen=True)
class IntegrationRiskScored:
    """Emitted when integration risk is computed for a task."""

    event_type: Literal["integration_risk_scored"] = "integration_risk_scored"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    target_head_sha: str = ""
    task_base_sha: str = ""
    task_commit_sha: str = ""
    risk_level: str = ""  # "none", "low", "medium", "high", "critical"
    claimed_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    python_overlap_detected: bool = False
    overlap_evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IntegrationOverlapDetected:
    """Emitted when semantic overlap is detected during integration."""

    event_type: Literal["integration_overlap_detected"] = "integration_overlap_detected"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntegrationCandidatePassed:
    """Emitted when an integration candidate passes validation."""

    event_type: Literal["integration_candidate_passed"] = "integration_candidate_passed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    candidate_sha: str = ""
    target_head_sha: str = ""
    passed: bool = True
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IntegrationCandidateFailed:
    """Emitted when an integration candidate fails validation."""

    event_type: Literal["integration_candidate_failed"] = "integration_candidate_failed"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    candidate_sha: str = ""
    target_head_sha: str = ""
    passed: bool = False
    failure_class: str = ""
    error_message: str = ""
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SemanticGateRejected:
    """Emitted when the semantic gate rejects a candidate."""

    event_type: Literal["semantic_gate_rejected"] = "semantic_gate_rejected"
    pane: str = ""
    plan_name: str = ""
    task_slug: str = ""
    gate_name: str = ""
    passed: bool = False
    failure_class: str = ""
    error_message: str = ""
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class UnknownEvent:
    """Fallback for unrecognized event types during deserialization."""

    event_type: Literal["unknown_event"] = "unknown_event"
    pane: str = ""
    event_name: str = ""  # Original event name from row
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredEvent:
    """Wraps a DgovEvent with storage metadata from SQLite."""

    id: int
    ts: str
    event: DgovEvent


# Union type for all events
DgovEvent = (
    RunStart
    | RunCompleted
    | EvtTaskDispatched
    | TaskDone
    | TaskFailed
    | TaskAbandoned
    | ReviewPass
    | ReviewFail
    | ReviewerVerdict
    | MergeCompleted
    | TaskMergeFailed
    | SettlementRetry
    | SelfReviewPassed
    | SelfReviewRejected
    | SelfReviewAutoPassed
    | SelfReviewFixStarted
    | SelfReviewError
    | IterationFork
    | GovernorResumed
    | ShutdownRequested
    | WorkerLog
    | IntegrationRiskScored
    | IntegrationOverlapDetected
    | IntegrationCandidatePassed
    | IntegrationCandidateFailed
    | SemanticGateRejected
    | UnknownEvent
)


# Mapping from event type string to dataclass
_EVENT_TYPE_MAP: dict[str, type[DgovEvent]] = {
    "run_start": RunStart,
    "run_completed": RunCompleted,
    "dag_task_dispatched": EvtTaskDispatched,
    "task_done": TaskDone,
    "task_failed": TaskFailed,
    "task_abandoned": TaskAbandoned,
    "review_pass": ReviewPass,
    "review_fail": ReviewFail,
    "reviewer_verdict": ReviewerVerdict,
    "merge_completed": MergeCompleted,
    "task_merge_failed": TaskMergeFailed,
    "settlement_retry": SettlementRetry,
    "self_review_passed": SelfReviewPassed,
    "self_review_rejected": SelfReviewRejected,
    "self_review_auto_passed": SelfReviewAutoPassed,
    "self_review_fix_started": SelfReviewFixStarted,
    "self_review_error": SelfReviewError,
    "iteration_fork": IterationFork,
    "dag_task_governor_resumed": GovernorResumed,
    "shutdown_requested": ShutdownRequested,
    "worker_log": WorkerLog,
    "integration_risk_scored": IntegrationRiskScored,
    "integration_overlap_detected": IntegrationOverlapDetected,
    "integration_candidate_passed": IntegrationCandidatePassed,
    "integration_candidate_failed": IntegrationCandidateFailed,
    "semantic_gate_rejected": SemanticGateRejected,
}


def serialize_event(event: DgovEvent) -> tuple[str, str, dict[str, Any]]:
    """Serialize a typed event into (event_name, pane, kwargs) for SQL writer.

    Extracts event_type, pane, and all non-default fields as kwargs.
    """
    event_name = event.event_type
    pane = event.pane

    # Build kwargs from all fields (excluding event_type, pane, and None values)
    kwargs: dict[str, Any] = {}
    for field_name, field_value in asdict(event).items():
        if field_name in ("event_type", "pane"):
            continue
        if field_value is None:
            continue
        kwargs[field_name] = field_value

    return event_name, pane, kwargs


def deserialize_event(row: dict[str, Any]) -> DgovEvent:
    """Dispatch on row['event'] and construct the typed event.

    Falls back to UnknownEvent for unrecognized event types.
    """
    event_name = row.get("event", "")
    event_class = _EVENT_TYPE_MAP.get(event_name)

    if event_class is None:
        # Unknown event type - return UnknownEvent with raw data
        return UnknownEvent(
            pane=row.get("pane", ""),
            event_name=event_name,
            raw_data={k: v for k, v in row.items() if k not in ("event", "pane")},
        )

    # Build kwargs from row data, filtering to only fields the dataclass accepts
    valid_fields = {f.name for f in fields(event_class)} - {"event_type", "pane"}
    kwargs: dict[str, Any] = {}
    for key, value in row.items():
        if key in ("event", "event_type", "id", "ts", "pane"):
            continue
        if key in valid_fields:
            kwargs[key] = value

    # Construct the event with pane from row
    pane = row.get("pane", "")
    return event_class(pane=pane, **kwargs)


__all__ = [
    "DgovEvent",
    "EvtTaskDispatched",
    "GovernorResumed",
    "IntegrationCandidateFailed",
    "IntegrationCandidatePassed",
    "IntegrationOverlapDetected",
    "IntegrationRiskScored",
    "IterationFork",
    "MergeCompleted",
    "ReviewFail",
    "ReviewPass",
    "ReviewerVerdict",
    "RunCompleted",
    "RunStart",
    "SelfReviewAutoPassed",
    "SelfReviewError",
    "SelfReviewFixStarted",
    "SelfReviewPassed",
    "SelfReviewRejected",
    "SemanticGateRejected",
    "SettlementRetry",
    "ShutdownRequested",
    "StoredEvent",
    "TaskAbandoned",
    "TaskDone",
    "TaskFailed",
    "TaskMergeFailed",
    "UnknownEvent",
    "WorkerLog",
    "deserialize_event",
    "serialize_event",
]
