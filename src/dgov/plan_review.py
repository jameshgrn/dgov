"""Plan review — post-hoc debrief of the last dgov run.

Pure data layer. Pulls events from the SQLite journal, deploy records
from deployed.jsonl, and commit shapes from git. Returns a PlanReview
snapshot that the CLI formatter can render (human or JSON).

Run-scoping: events persist across runs unless --restart is passed, so
review uses the latest `run_start` event per plan as its lower bound.
If no run_start exists (plan was never run, or ran before the marker
was introduced), the whole event log for that plan is considered.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from dgov.deploy_log import read as read_deploy_log
from dgov.event_types import (
    DgovEvent,
    EvtTaskDispatched,
    IntegrationCandidateFailed,
    IntegrationCandidatePassed,
    IntegrationOverlapDetected,
    IntegrationRiskScored,
    IterationFork,
    MergeCompleted,
    ReviewFail,
    RunCompleted,
    RunStart,
    SelfReviewAutoPassed,
    SelfReviewError,
    SelfReviewFixStarted,
    SelfReviewPassed,
    SelfReviewRejected,
    SemanticGateRejected,
    SettlementPhaseCompleted,
    SettlementPhaseStarted,
    SettlementRetry,
    TaskAbandoned,
    TaskDone,
    TaskFailed,
    TaskMergeFailed,
    WorkerLog,
    deserialize_event,
)
from dgov.persistence import read_events
from dgov.repo_snapshot import format_structural_offender_report
from dgov.semantic_settlement import describe_evidence_payload

_log = logging.getLogger(__name__)
_ITERATION_EXHAUSTED_RE = re.compile(r"Exceeded max iterations \((?P<budget>\d+)\)", re.IGNORECASE)
_RUNS_LOG_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s+(\S+)")
_RUNS_LOG_STATUS_RE = re.compile(r"^\[[^\]]+\]\s+\S+\s+\([^)]+\)\s+—\s+(\w+)")
_RUNS_LOG_SENTRUX_RE = re.compile(r"sentrux:\s*(\d+)\s*->\s*(\d+|None)")
_RUNS_LOG_STATUS_ALIASES = {
    "ok": "complete",
    "warn": "degraded",
    "fail": "failed",
}
_RUNS_LOG_RUN_STATUSES = frozenset({"complete", "degraded", "failed", "partial"})
_RUNS_LOG_FIELD_KEYS = {
    "branch_verification_status": "branch_verification_status",
    "branch_verification_error": "branch_verification_error",
    "sentrux_error": "sentrux_error",
    "sentrux_offenders": "sentrux_offender_summary",
}
_VERDICT_HINTS = {
    "empty_diff": (
        "worker produced no changes — Orient/Edit/Verify is probably unclear, "
        "or the edit target is already in the desired state"
    ),
    "lint_fail": "autofix couldn't fix — lint/format failure needs manual intervention",
    "format_fail": "autofix couldn't fix — lint/format failure needs manual intervention",
    "test_fail": "tests failed after the edit — check Verify commands against the plan",
    "review_hook_fail": "a project review_hook rejected the commit — see error for which hook",
}
_WorkerLogHandler = Callable[[object, dict], None]


@dataclass(frozen=True)
class _EventWithId:
    """Wrapper for a typed event with its database id and timestamp.

    Preserves metadata (id, ts) that isn't part of the typed event dataclass.
    """

    id: int
    ts: str
    event: DgovEvent


def _convert_events(raw_events: list[dict[str, Any]]) -> list[_EventWithId]:
    """Convert raw dict events from read_events() to typed events with id/ts metadata.

    UnknownEvent types are preserved (passed through) so unrecognized events
    can be handled the same way as before (skipped).
    """
    result: list[_EventWithId] = []
    for row in raw_events:
        event_id = row.get("id", 0)
        ts = row.get("ts", "")
        typed_event = deserialize_event(row)
        result.append(_EventWithId(id=event_id, ts=ts, event=typed_event))
    return result


UnitStatus = Literal["deployed", "failed", "active", "pending", "not_run"]
SettlementResult = Literal["ok", "ok_retried", "rejected", "n/a"]


@dataclass(frozen=True)
class DiffStat:
    """Diff shape summary derived from `git show --numstat`."""

    files_changed: int
    insertions: int
    deletions: int

    def summary(self) -> str:
        plural = "s" if self.files_changed != 1 else ""
        return f"{self.files_changed} file{plural}, +{self.insertions} -{self.deletions}"


@dataclass(frozen=True)
class SettlementPhaseTiming:
    """Completed settlement phase timing for one unit."""

    phase: str
    duration_s: float
    status: str
    error: str | None = None


@dataclass(frozen=True)
class UnitReview:
    """Per-unit review snapshot."""

    unit: str
    summary: str
    status: UnitStatus
    # Phase detail for in-flight tasks (e.g., "integration", "semantic_gate", "merge")
    # Only populated when status is "active" and settlement phase events exist
    phase: str | None = None
    phase_timings: tuple[SettlementPhaseTiming, ...] = ()
    agent: str = ""
    # Deployment info (populated for status == "deployed")
    commit_sha: str | None = None
    commit_message: str | None = None
    commit_ts: str | None = None
    diff_stat: DiffStat | None = None
    landed_files: tuple[str, ...] = ()
    full_diff: str | None = None  # Populated only when caller asks for it
    # Execution info (populated for any unit that actually ran)
    duration_s: float | None = None
    # Legacy JSON/API field. Mirrors tool_calls until true model-turn telemetry exists.
    iterations: int | None = None
    tool_calls: int | None = None
    attempts: int = 0
    settlement: SettlementResult = "n/a"
    done_summary: str | None = None
    worker_note_mismatches: tuple[str, ...] = ()
    thoughts: tuple[str, ...] = ()  # All worker thoughts in order
    activity: tuple[dict, ...] = ()  # All tool calls in order
    # Count of tool result events with status="failed" that the worker
    # recovered from en route to a deployed commit. Zero for failed units
    # (no recovery occurred) and for units with no tool activity at all.
    self_corrections: int = 0
    # Token usage (from task_done/task_failed events)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Fork and self-review telemetry
    fork_depth: int = 0  # Number of clean-context forks that occurred
    self_review_outcome: str | None = None  # passed | rejected | auto_passed | error | None
    # Failure info (populated for status == "failed")
    reject_verdict: str | None = None
    error: str | None = None
    last_thought: str | None = None
    hint: str | None = None
    # Integration risk telemetry (populated when semantic settlement events exist)
    integration_risk_level: str | None = None  # RiskLevel value
    # True if python_overlap_detected or any overlap evidence
    integration_risk_detected: bool = False
    integration_candidate_passed: bool | None = None  # None if no candidate validation
    integration_failure_class: str | None = None  # FailureClass value
    integration_claimed_files: tuple[str, ...] = ()
    integration_changed_files: tuple[str, ...] = ()
    integration_gate_name: str | None = None
    integration_error: str | None = None
    integration_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanReview:
    """Full plan debrief."""

    plan_name: str
    source_dir: Path | None
    last_run_ts: str | None
    last_run_duration_s: float | None
    units: list[UnitReview] = field(default_factory=list)
    # Run-level fields extracted from run_completed event or runs.log fallback
    run_status: str | None = None  # "complete", "degraded", "partial", "failed"
    sentrux_degradation: bool | None = None
    sentrux_quality_before: int | None = None
    sentrux_quality_after: int | None = None
    sentrux_error: str | None = None
    sentrux_offender_summary: str | None = None
    branch_verification_status: str | None = None
    branch_verification_error: str | None = None

    @property
    def deployed_count(self) -> int:
        return sum(1 for u in self.units if u.status == "deployed")

    @property
    def failed_count(self) -> int:
        return sum(1 for u in self.units if u.status == "failed")

    @property
    def active_count(self) -> int:
        return sum(1 for u in self.units if u.status == "active")

    @property
    def pending_count(self) -> int:
        return sum(1 for u in self.units if u.status in ("pending", "not_run"))


@dataclass(frozen=True)
class RunEnvelope:
    """Lightweight run-level snapshot for status and follow-up decisions."""

    plan_name: str
    last_run_ts: str | None
    run_status: str | None = None
    sentrux_degradation: bool | None = None
    sentrux_quality_before: int | None = None
    sentrux_quality_after: int | None = None
    sentrux_error: str | None = None
    sentrux_offender_summary: str | None = None
    branch_verification_status: str | None = None
    branch_verification_error: str | None = None


# ---------------------------------------------------------------------------
# Hint synthesis
# ---------------------------------------------------------------------------


def synthesize_hint(
    verdict: str | None,
    error: str | None,
    iterations: int | None,
    iteration_budget: int | None,
) -> str | None:
    """Build a short actionable hint from a failure's shape.

    Pure lookup over verdict + a few context signals. Returns None when
    nothing useful can be said so the formatter can fall back silently.
    """
    _ = iterations  # Legacy tool-call count; not a model-turn budget signal.
    exhausted_hint = _iteration_exhaustion_hint(error, iteration_budget)
    if exhausted_hint is not None:
        return exhausted_hint
    return _verdict_hint(verdict, error)


def _iteration_exhaustion_hint(error: str | None, iteration_budget: int | None) -> str | None:
    if error:
        match = _ITERATION_EXHAUSTED_RE.search(error)
        if match:
            budget = match.group("budget")
            return (
                f"worker hit the {budget}-iteration model-turn budget — task is probably "
                "too large; split it or clarify the Edit section"
            )
        if "exceeded max iterations" in error.lower() and iteration_budget:
            return (
                f"worker hit the {iteration_budget}-iteration model-turn budget — task is "
                "probably too large; split it or clarify the Edit section"
            )
    return None


def _verdict_hint(verdict: str | None, error: str | None) -> str | None:
    if verdict is None:
        return None
    v = verdict.lower()
    if v == "scope_violation":
        return _scope_violation_hint(error)
    return _VERDICT_HINTS.get(v)


def _scope_violation_hint(error: str | None) -> str:
    if error and ":" in error:
        return "worker touched unclaimed files — add them to files.edit OR split into a new task"
    return "worker touched unclaimed files — add them to files.edit"


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def _load_plan_units(compiled_path: Path) -> dict[str, dict]:
    """Read tasks section from a compiled plan TOML. Empty dict if missing."""
    if not compiled_path.exists():
        return {}
    raw = tomllib.loads(compiled_path.read_text())
    return raw.get("tasks", {}) or {}


def _plan_name_from_compiled(compiled_path: Path) -> str | None:
    if not compiled_path.exists():
        return None
    raw = tomllib.loads(compiled_path.read_text())
    return raw.get("plan", {}).get("name")


def _find_run_start_id(typed_events: list[_EventWithId], plan_name: str) -> int:
    """Return the id of the latest run_start event for this plan, or 0."""
    latest = 0
    for ev in typed_events:
        if isinstance(ev.event, RunStart) and ev.event.plan_name == plan_name and ev.id > latest:
            latest = ev.id
    return latest


def _iso_to_epoch(ts: str) -> float | None:
    """Parse an ISO timestamp emitted by emit_event. Returns None on failure."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _git_show_stat(project_root: str, sha: str) -> DiffStat | None:
    """Run `git show --numstat` for a commit and return a DiffStat."""
    try:
        result = subprocess.run(
            ["git", "show", "--numstat", "--format=", sha],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    files = 0
    ins = 0
    dels = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        # Binary files show "-\t-\tpath"
        files += 1
        if parts[0].isdigit():
            ins += int(parts[0])
        if parts[1].isdigit():
            dels += int(parts[1])
    return DiffStat(files_changed=files, insertions=ins, deletions=dels)


def _git_show_paths(project_root: str, sha: str) -> tuple[str, ...] | None:
    """Return changed paths for a commit in display order."""
    try:
        result = subprocess.run(
            ["git", "show", "--numstat", "--format=", sha],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    paths: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[2]
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _git_show_message(project_root: str, sha: str) -> str | None:
    """Return the subject line of a commit message."""
    try:
        result = subprocess.run(
            ["git", "show", "--no-patch", "--format=%s", sha],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _git_show_full_diff(project_root: str, sha: str) -> str | None:
    """Return the full patch text for a commit."""
    try:
        result = subprocess.run(
            ["git", "show", sha],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return result.stdout or None


_PATH_TOKEN_SPLIT_RE = re.compile(r"""[\s'",:;!?()[\]{}`]+""")
_ROOT_FILE_SUFFIXES = {
    "csv",
    "json",
    "md",
    "py",
    "pyi",
    "sh",
    "toml",
    "txt",
    "yaml",
    "yml",
}

# Change-indicating words that suggest the worker is claiming to have modified a file.
# These must appear within a small window before the path to count as a change claim.
_CHANGE_VERBS = frozenset({
    "add",
    "added",
    "change",
    "changed",
    "create",
    "created",
    "edit",
    "edited",
    "fix",
    "fixed",
    "implement",
    "implemented",
    "modify",
    "modified",
    "remove",
    "removed",
    "rename",
    "renamed",
    "update",
    "updated",
    "write",
    "wrote",
})

# Non-change indicators that suggest the path is mentioned in a verification/reference context.
# These suppress change-claim detection when they appear immediately before the path.
_NON_CHANGE_CONTEXT_WORDS = frozenset({
    "verify",
    "verified",
    "check",
    "checked",
    "confirm",
    "confirmed",
    "ensure",
    "ensured",
    "test",
    "tested",
    "see",
    "reference",
    "import",
    "from",
    "using",
    "via",
    "in",
    "at",
    "by",
    "read",
    "reading",
})

# Window size for context detection: how many tokens before the path to check for change verbs.
_CHANGE_CONTEXT_WINDOW = 4


def _extract_path_mentions(text: str) -> tuple[str, ...]:
    """Return ordered unique file-like path mentions that appear in change-claim context.

    Filters out paths that appear to be mentioned only in verification, reference,
    or other non-change contexts. Only returns paths where a change-indicating word
    appears within the preceding token window.
    """
    seen: set[str] = set()
    paths: list[str] = []
    tokens = _path_mention_tokens(text)

    for index, raw in enumerate(tokens):
        path = _normalize_path_mention(raw)
        if not _path_mention_is_candidate(path):
            continue
        if path in seen:
            continue
        if _path_mention_has_change_context(tokens, index):
            seen.add(path)
            paths.append(path)

    return tuple(paths)


def _path_mention_tokens(text: str) -> list[str]:
    return [token.strip() for token in _PATH_TOKEN_SPLIT_RE.split(text) if token.strip()]


def _normalize_path_mention(raw: str) -> str:
    path = raw.strip().strip(".").strip("*_`<>")
    return path[2:] if path.startswith("./") else path


def _path_mention_is_candidate(path: str) -> bool:
    if not path or "." not in path:
        return False
    suffix = path.rsplit(".", 1)[1].lower()
    return "/" in path or suffix in _ROOT_FILE_SUFFIXES


def _path_mention_has_change_context(tokens: list[str], index: int) -> bool:
    preceding_tokens = _preceding_path_mention_tokens(tokens, index)
    return _has_change_verb(preceding_tokens) and not _has_non_change_context(preceding_tokens)


def _preceding_path_mention_tokens(tokens: list[str], index: int) -> list[str]:
    start_idx = max(0, index - _CHANGE_CONTEXT_WINDOW)
    return tokens[start_idx:index]


def _has_change_verb(tokens: list[str]) -> bool:
    return any(token.lower() in _CHANGE_VERBS for token in tokens)


def _has_non_change_context(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[-1].lower() in _NON_CHANGE_CONTEXT_WORDS


def _worker_note_mismatches(
    done_summary: str | None, landed_files: tuple[str, ...]
) -> tuple[str, ...]:
    """Return file-like mentions in a worker note that are absent from the landed diff."""
    if not done_summary or not landed_files:
        return ()
    landed = {path.lstrip("./") for path in landed_files}
    return tuple(path for path in _extract_path_mentions(done_summary) if path not in landed)


# ---------------------------------------------------------------------------
# Per-unit event rollup
# ---------------------------------------------------------------------------


def _extract_review_fail_fields(ev: _EventWithId, state: dict) -> None:
    """Extract verdict and error from review_fail event."""
    if isinstance(ev.event, ReviewFail):
        state["reject_verdict"] = ev.event.verdict or state["reject_verdict"]
        err = ev.event.error
        if isinstance(err, str):
            state["error"] = err


def _apply_terminal_event(ev: _EventWithId, state: dict) -> None:
    """Apply terminal event effects to state."""
    state["terminal_ts"] = ev.ts
    if isinstance(ev.event, MergeCompleted):
        state["merged_in_run"] = True
    elif isinstance(ev.event, (TaskMergeFailed, ReviewFail, TaskAbandoned)):
        state["failed_in_run"] = True
        if isinstance(ev.event, ReviewFail):
            _extract_review_fail_fields(ev, state)


def _apply_task_token_event(ev: _EventWithId, state: dict) -> None:
    """Accumulate token usage from task_done/task_failed events."""
    if not isinstance(ev.event, (TaskDone, TaskFailed)):
        return
    for token_field in ("prompt_tokens", "completion_tokens"):
        val = getattr(ev.event, token_field)
        if isinstance(val, int):
            state[token_field] = state.get(token_field, 0) + val
        elif isinstance(val, str):
            try:
                state[token_field] = state.get(token_field, 0) + int(val)
            except ValueError:
                evt_type = ev.event.event_type
                _log.warning("Non-numeric %s value %r in %s event", token_field, val, evt_type)


def _apply_self_review_event(ev: _EventWithId, state: dict) -> bool:
    """Apply self-review lifecycle events. Returns True when handled."""
    if isinstance(ev.event, SelfReviewFixStarted):
        return True
    if isinstance(ev.event, SelfReviewPassed):
        state["self_review_outcome"] = "passed"
        return True
    if isinstance(ev.event, SelfReviewRejected):
        state["self_review_outcome"] = "rejected"
        return True
    if isinstance(ev.event, SelfReviewAutoPassed):
        state["self_review_outcome"] = "auto_passed"
        return True
    if isinstance(ev.event, SelfReviewError):
        state["self_review_outcome"] = "error"
        return True
    return False


def _apply_settlement_phase_event(ev: _EventWithId, state: dict) -> bool:
    """Apply settlement phase start/completion telemetry. Returns True when handled."""
    if isinstance(ev.event, SettlementPhaseStarted):
        phase = ev.event.phase
        if isinstance(phase, str) and phase:
            state["phase"] = phase
        return True
    if isinstance(ev.event, SettlementPhaseCompleted):
        phase = ev.event.phase
        if isinstance(phase, str) and phase:
            duration = ev.event.duration_s
            if isinstance(duration, int | float):
                state["phase_timings"].append(
                    SettlementPhaseTiming(
                        phase=phase,
                        duration_s=float(duration),
                        status=ev.event.status,
                        error=ev.event.error,
                    )
                )
        state["phase"] = None
        return True
    return False


def _apply_lifecycle_event(ev: _EventWithId, state: dict) -> None:
    """Handle lifecycle events: dispatch, terminal, settlement_retry, review_fail."""
    if isinstance(ev.event, EvtTaskDispatched) and state["dispatched_ts"] is None:
        state["dispatched_ts"] = ev.ts
        return
    _apply_task_token_event(ev, state)
    if isinstance(ev.event, (MergeCompleted, TaskMergeFailed, ReviewFail, TaskAbandoned)):
        _apply_terminal_event(ev, state)
        state["phase"] = None
        return
    if isinstance(ev.event, SettlementRetry):
        state["settlement_retries"] = state["settlement_retries"] + 1
        # settlement_retry resets merged-in-run until a later merge_completed.
        state["merged_in_run"] = False
        state["failed_in_run"] = False
        return
    if isinstance(ev.event, IterationFork):
        depth = ev.event.fork_depth
        if isinstance(depth, int):
            state["fork_depth"] = max(state["fork_depth"], depth)
        return
    if _apply_self_review_event(ev, state):
        return
    if _apply_settlement_phase_event(ev, state):
        return


def _record_worker_thought(content: object, state: dict) -> None:
    if isinstance(content, str):
        state["thoughts"].append(content)


def _record_worker_call(content: object, state: dict) -> None:
    if isinstance(content, dict):
        state["tool_calls"] = state["tool_calls"] + 1
        state["activity"].append(content)


def _record_worker_result(content: object, state: dict) -> None:
    if isinstance(content, dict) and cast("dict[str, object]", content).get("status") == "failed":
        state["failed_tool_calls"] = state["failed_tool_calls"] + 1


def _record_worker_done(content: object, state: dict) -> None:
    if isinstance(content, str):
        state["done_summary"] = content


def _record_worker_error(content: object, state: dict) -> None:
    if isinstance(content, str) and state["error"] is None:
        state["error"] = content


_WORKER_LOG_HANDLERS: dict[str, _WorkerLogHandler] = {
    "thought": _record_worker_thought,
    "call": _record_worker_call,
    "result": _record_worker_result,
    "done": _record_worker_done,
    "error": _record_worker_error,
}


def _apply_worker_log_event(ev: _EventWithId, state: dict) -> None:
    """Handle worker_log events: thoughts, calls, results, done, error."""
    if not isinstance(ev.event, WorkerLog):
        return
    handler = _WORKER_LOG_HANDLERS.get(ev.event.log_type)
    if handler is not None:
        handler(ev.event.content, state)


def _apply_integration_risk_scored(event: IntegrationRiskScored, state: dict) -> None:
    risk_level = event.risk_level
    if isinstance(risk_level, str):
        state["integration_risk_level"] = risk_level
    state["integration_claimed_files"] = tuple(event.claimed_files or ())
    state["integration_changed_files"] = tuple(event.changed_files or ())
    if event.python_overlap_detected is True:
        state["integration_risk_detected"] = True
    if event.overlap_evidence and len(event.overlap_evidence) > 0:
        state["integration_risk_detected"] = True
        _append_integration_evidence(state, event.overlap_evidence)


def _append_integration_evidence(state: dict, evidence_payload: object) -> None:
    payload: tuple[dict[str, Any], ...]
    if isinstance(evidence_payload, dict):
        payload = (cast("dict[str, Any]", evidence_payload),)
    elif isinstance(evidence_payload, tuple | list):
        payload = tuple(
            cast("dict[str, Any]", item) for item in evidence_payload if isinstance(item, dict)
        )
    else:
        payload = ()
    if not payload:
        return

    known = set(state["integration_evidence"])
    for description in describe_evidence_payload(payload):
        if description not in known:
            state["integration_evidence"].append(description)
            known.add(description)


def _apply_integration_failure(
    event: IntegrationCandidateFailed | SemanticGateRejected,
    state: dict,
) -> None:
    state["integration_candidate_passed"] = False
    failure_class = event.failure_class
    if isinstance(failure_class, str):
        state["integration_failure_class"] = failure_class
    error_message = event.error_message
    if isinstance(error_message, str) and error_message:
        state["integration_error"] = error_message
    if isinstance(event, SemanticGateRejected) and event.gate_name:
        state["integration_gate_name"] = event.gate_name
    _append_integration_evidence(state, event.evidence)


def _apply_semantic_settlement_event(ev: _EventWithId, state: dict) -> None:
    """Handle semantic settlement events: risk scoring, candidate validation, gates."""
    if isinstance(ev.event, IntegrationRiskScored):
        _apply_integration_risk_scored(ev.event, state)
    elif isinstance(ev.event, IntegrationOverlapDetected):
        state["integration_risk_detected"] = True
        _append_integration_evidence(state, ev.event.evidence)
    elif isinstance(ev.event, IntegrationCandidatePassed):
        state["integration_candidate_passed"] = True
        _append_integration_evidence(state, ev.event.evidence)
    elif isinstance(ev.event, (IntegrationCandidateFailed, SemanticGateRejected)):
        _apply_integration_failure(ev.event, state)


def _initial_unit_rollup_state() -> dict:
    return {
        "thoughts": [],
        "activity": [],
        "tool_calls": 0,
        "failed_tool_calls": 0,
        "done_summary": None,
        "error": None,
        "reject_verdict": None,
        "settlement_retries": 0,
        "dispatched_ts": None,
        "terminal_ts": None,
        "merge_sha": None,
        "merged_in_run": False,
        "failed_in_run": False,
        # Fork and self-review telemetry
        "fork_depth": 0,
        "self_review_outcome": None,
        # Integration risk telemetry
        "integration_risk_level": None,
        "integration_risk_detected": False,
        "integration_candidate_passed": None,
        "integration_failure_class": None,
        "integration_claimed_files": (),
        "integration_changed_files": (),
        "integration_gate_name": None,
        "integration_error": None,
        "integration_evidence": [],
        # Settlement phase tracking
        "phase": None,
        "phase_timings": [],
    }


def _apply_unit_event(ev: _EventWithId, state: dict) -> None:
    if isinstance(ev.event, WorkerLog):
        _apply_worker_log_event(ev, state)
        return
    _apply_lifecycle_event(ev, state)
    _apply_semantic_settlement_event(ev, state)


def _unit_duration(state: dict) -> float | None:
    dispatched_ts = state["dispatched_ts"]
    terminal_ts = state["terminal_ts"]
    if dispatched_ts and terminal_ts:
        start = _iso_to_epoch(dispatched_ts)
        end = _iso_to_epoch(terminal_ts)
        if start is not None and end is not None:
            return max(0.0, end - start)
    return None


def _unit_rollup_dict(state: dict, unit_events: list[dict]) -> dict:
    ran_in_run = bool(unit_events)
    tool_calls = state["tool_calls"]
    return {
        "thoughts": state["thoughts"],
        "activity": state["activity"],
        "iterations": tool_calls if tool_calls > 0 else None,
        "tool_calls": tool_calls if tool_calls > 0 else None,
        "failed_tool_calls": state["failed_tool_calls"],
        "done_summary": state["done_summary"],
        "error": state["error"],
        "reject_verdict": state["reject_verdict"],
        "settlement_retries": state["settlement_retries"],
        "duration_s": _unit_duration(state),
        "merge_sha": state["merge_sha"],
        "merged_in_run": state["merged_in_run"],
        "failed_in_run": state["failed_in_run"],
        "ran_in_run": ran_in_run,
        # Token usage
        "prompt_tokens": state.get("prompt_tokens") or None,
        "completion_tokens": state.get("completion_tokens") or None,
        # Fork and self-review telemetry
        "fork_depth": state["fork_depth"],
        "self_review_outcome": state["self_review_outcome"],
        # Integration risk telemetry
        "integration_risk_level": state["integration_risk_level"],
        "integration_risk_detected": state["integration_risk_detected"],
        "integration_candidate_passed": state["integration_candidate_passed"],
        "integration_failure_class": state["integration_failure_class"],
        "integration_claimed_files": state["integration_claimed_files"],
        "integration_changed_files": state["integration_changed_files"],
        "integration_gate_name": state["integration_gate_name"],
        "integration_error": state["integration_error"],
        "integration_evidence": tuple(state["integration_evidence"]),
        # Settlement phase
        "phase": state["phase"],
        "phase_timings": tuple(state["phase_timings"]),
    }


def _rollup_unit_events(unit_events: list[dict]) -> dict:
    """Collapse a unit's events into a rollup dict used by _build_unit_review."""
    state = _initial_unit_rollup_state()
    for ev in _convert_events(unit_events):
        _apply_unit_event(ev, state)
    return _unit_rollup_dict(state, unit_events)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _settlement_result(
    status: UnitStatus, settlement_retries: int, reject_verdict: str | None
) -> SettlementResult:
    if status == "deployed":
        return "ok_retried" if settlement_retries > 0 else "ok"
    if status == "failed":
        return "rejected" if reject_verdict else "n/a"
    return "n/a"


def _compute_unit_status(
    ran_in_run: bool,
    merged_in_run: bool,
    failed_in_run: bool,
    has_error: bool,
    has_reject_verdict: bool,
    deploy_record,
) -> UnitStatus:
    """Determine unit status from rollup state and deploy record.

    Status reflects the CURRENT run's outcome, not any historical deploy.
    A unit that merged in an earlier run but failed in this run is "failed".
    A unit in flight during the current run is "active".
    A unit that didn't run at all in this run falls back to deploy_log.
    """
    if ran_in_run:
        if merged_in_run:
            return "deployed"
        if failed_in_run or has_error or has_reject_verdict:
            return "failed"
        return "active"
    if deploy_record is not None:
        return "deployed"
    return "not_run"


def _fetch_deployed_commit_info(
    project_root: str,
    deploy_record,
    done_summary: str | None,
    include_full_diff: bool,
) -> dict:
    """Fetch git-derived info for a deployed unit.

    Returns a dict with: commit_sha, commit_ts, commit_message, diff_stat,
    landed_files, full_diff, worker_note_mismatches.
    """
    commit_sha = deploy_record.sha
    commit_ts = deploy_record.ts
    commit_message = _git_show_message(project_root, commit_sha)
    diff_stat = _git_show_stat(project_root, commit_sha)
    landed_files = _git_show_paths(project_root, commit_sha) or ()
    worker_note_mismatches = _worker_note_mismatches(done_summary, landed_files)
    full_diff = None
    if include_full_diff:
        full_diff = _git_show_full_diff(project_root, commit_sha)
    return {
        "commit_sha": commit_sha,
        "commit_ts": commit_ts,
        "commit_message": commit_message,
        "diff_stat": diff_stat,
        "landed_files": landed_files,
        "full_diff": full_diff,
        "worker_note_mismatches": worker_note_mismatches,
    }


def _empty_commit_info() -> dict:
    """Return empty commit info structure for non-deployed units."""
    return {
        "commit_sha": None,
        "commit_ts": None,
        "commit_message": None,
        "diff_stat": None,
        "landed_files": (),
        "full_diff": None,
        "worker_note_mismatches": (),
    }


def _compute_self_corrections(failed_tool_calls: int, status: UnitStatus) -> int:
    """Count self-corrections: only count on deployed units (recovered failures).

    Failed units did not recover, so they show 0.
    """
    return failed_tool_calls if status == "deployed" else 0


def _compute_attempts(unit_events: list[dict], settlement_retries: int) -> int:
    """Compute total attempts including settlement retries."""
    return 1 + settlement_retries if unit_events else 0


def _compute_last_thought(thoughts: list[str], status: UnitStatus) -> str | None:
    """Return the last thought for failed or active units."""
    if not thoughts:
        return None
    if status in ("failed", "active"):
        return thoughts[-1]
    return None


def _unit_commit_info(
    status: UnitStatus,
    deploy_record,
    project_root: str,
    done_summary: str | None,
    include_full_diff: bool,
) -> dict:
    if status == "deployed" and deploy_record is not None:
        return _fetch_deployed_commit_info(
            project_root,
            deploy_record,
            done_summary,
            include_full_diff,
        )
    return _empty_commit_info()


def _failed_unit_hint(
    status: UnitStatus,
    task_data: dict,
    rollup: dict,
    iteration_budget: int | None,
) -> str | None:
    if status != "failed":
        return None
    unit_iteration_budget = task_data.get("iteration_budget", iteration_budget)
    return synthesize_hint(
        rollup["reject_verdict"],
        rollup["error"],
        rollup["iterations"],
        unit_iteration_budget,
    )


def _derived_review_metrics(
    task_data: dict,
    unit_events: list[dict],
    rollup: dict,
    status: UnitStatus,
    iteration_budget: int | None,
) -> dict[str, Any]:
    return {
        "attempts": _compute_attempts(unit_events, rollup["settlement_retries"]),
        "settlement": _settlement_result(
            status,
            rollup["settlement_retries"],
            rollup["reject_verdict"],
        ),
        "self_corrections": _compute_self_corrections(rollup["failed_tool_calls"], status),
        "last_thought": _compute_last_thought(rollup["thoughts"], status),
        "hint": _failed_unit_hint(status, task_data, rollup, iteration_budget),
    }


def _compute_derived_review_state(
    unit_id: str,
    task_data: dict,
    deploy_record,
    unit_events: list[dict],
    project_root: str,
    include_full_diff: bool,
    iteration_budget: int | None,
) -> dict[str, Any]:
    """Compute all derived review state from unit events and metadata.

    Returns a dict containing: rollup, status, commit_info, attempts,
    settlement, self_corrections, last_thought, hint.
    """
    rollup = _rollup_unit_events(unit_events)
    status = _compute_unit_status(
        ran_in_run=rollup["ran_in_run"],
        merged_in_run=rollup["merged_in_run"],
        failed_in_run=rollup["failed_in_run"],
        has_error=bool(rollup["error"]),
        has_reject_verdict=bool(rollup["reject_verdict"]),
        deploy_record=deploy_record,
    )
    commit_info = _unit_commit_info(
        status,
        deploy_record,
        project_root,
        rollup["done_summary"],
        include_full_diff,
    )

    return {
        "rollup": rollup,
        "status": status,
        "commit_info": commit_info,
        **_derived_review_metrics(
            task_data,
            unit_events,
            rollup,
            status,
            iteration_budget,
        ),
    }


def _build_core_review_fields(
    unit_id: str, task_data: dict, rollup: dict, status: str
) -> dict[str, Any]:
    """Build core UnitReview fields from task data and rollup."""
    return {
        "unit": unit_id,
        "summary": task_data.get("summary", ""),
        "status": status,
        "phase": rollup["phase"] if status == "active" else None,
        "phase_timings": rollup["phase_timings"],
        "agent": task_data.get("agent", ""),
        "duration_s": rollup["duration_s"],
        "iterations": rollup["iterations"],
        "tool_calls": rollup["tool_calls"],
        "done_summary": rollup["done_summary"],
        "thoughts": tuple(rollup["thoughts"]),
        "activity": tuple(rollup["activity"]),
        "prompt_tokens": rollup["prompt_tokens"],
        "completion_tokens": rollup["completion_tokens"],
        "fork_depth": rollup["fork_depth"],
        "self_review_outcome": rollup["self_review_outcome"],
    }


def _build_commit_fields(commit_info: dict) -> dict[str, Any]:
    """Build commit-related UnitReview fields from commit_info."""
    return {
        "commit_sha": commit_info["commit_sha"],
        "commit_message": commit_info["commit_message"],
        "commit_ts": commit_info["commit_ts"],
        "diff_stat": commit_info["diff_stat"],
        "landed_files": commit_info["landed_files"],
        "full_diff": commit_info["full_diff"],
        "worker_note_mismatches": commit_info["worker_note_mismatches"],
    }


def _build_failure_fields(
    rollup: dict, last_thought: str | None, hint: str | None
) -> dict[str, Any]:
    """Build failure-related UnitReview fields."""
    return {
        "reject_verdict": rollup["reject_verdict"],
        "error": rollup["error"],
        "last_thought": last_thought,
        "hint": hint,
    }


def _build_integration_fields(rollup: dict) -> dict[str, Any]:
    """Build integration risk-related UnitReview fields."""
    return {
        "integration_risk_level": rollup["integration_risk_level"],
        "integration_risk_detected": rollup["integration_risk_detected"],
        "integration_candidate_passed": rollup["integration_candidate_passed"],
        "integration_failure_class": rollup["integration_failure_class"],
        "integration_claimed_files": rollup["integration_claimed_files"],
        "integration_changed_files": rollup["integration_changed_files"],
        "integration_gate_name": rollup["integration_gate_name"],
        "integration_error": rollup["integration_error"],
        "integration_evidence": rollup["integration_evidence"],
    }


def _build_unit_review_kwargs(
    unit_id: str,
    task_data: dict,
    deploy_record,
    unit_events: list[dict],
    project_root: str,
    include_full_diff: bool,
    iteration_budget: int | None,
) -> dict[str, Any]:
    """Build kwargs dict for UnitReview constructor from unit events and metadata.

    Pure data builder: computes all derived values (status, commit info, attempts,
    settlement, etc.) and returns them as a dict for UnitReview construction.
    """
    derived = _compute_derived_review_state(
        unit_id,
        task_data,
        deploy_record,
        unit_events,
        project_root,
        include_full_diff,
        iteration_budget,
    )
    rollup = derived["rollup"]
    status = derived["status"]
    commit_info = derived["commit_info"]

    return {
        **_build_core_review_fields(unit_id, task_data, rollup, status),
        **_build_commit_fields(commit_info),
        "attempts": derived["attempts"],
        "settlement": derived["settlement"],
        "self_corrections": derived["self_corrections"],
        **_build_failure_fields(rollup, derived["last_thought"], derived["hint"]),
        **_build_integration_fields(rollup),
    }


def _build_unit_review(
    unit_id: str,
    task_data: dict,
    deploy_record,
    unit_events: list[dict],
    project_root: str,
    include_full_diff: bool,
    iteration_budget: int | None,
) -> UnitReview:
    """Build a UnitReview from unit events and metadata."""
    kwargs = _build_unit_review_kwargs(
        unit_id,
        task_data,
        deploy_record,
        unit_events,
        project_root,
        include_full_diff,
        iteration_budget,
    )
    return UnitReview(**kwargs)


def _fetch_worker_events_for_unit(
    project_root: str,
    plan_name: str,
    uid: str,
    lifecycle: list[dict],
    run_start_id: int,
) -> list[dict]:
    """Fetch worker_log events for a unit, with fallback to task-only scoped query.

    Primary fetch uses plan_name + task_slug. If empty, falls back to task_slug-only
    with optional pane-based filtering.
    """
    raw_events = read_events(
        project_root,
        plan_name=plan_name,
        task_slug=uid,
        after_id=run_start_id,
    )
    worker_events = [ev for ev in raw_events if ev.get("event") == "worker_log"]
    if worker_events:
        return worker_events

    # Fallback: fetch by task_slug only, then filter by known panes if available.
    fallback_events = [
        ev
        for ev in read_events(project_root, task_slug=uid, after_id=run_start_id)
        if ev.get("event") == "worker_log"
    ]
    allowed_panes = {
        pane for pane in (ev.get("pane") for ev in lifecycle) if isinstance(pane, str)
    }
    if allowed_panes:
        fallback_events = [ev for ev in fallback_events if ev.get("pane") in allowed_panes]
    return fallback_events


def _combine_unit_events(lifecycle: list[dict], worker_events: list[dict]) -> list[dict]:
    """Interleave lifecycle and worker log events chronologically by id."""
    combined = worker_events + lifecycle
    combined.sort(key=lambda e: e.get("id", 0))
    return combined


def _extract_run_start_ts(typed_events: list[_EventWithId], run_start_id: int) -> str | None:
    """Extract timestamp of the run_start event matching the given id."""
    for ev in typed_events:
        if isinstance(ev.event, RunStart) and ev.id == run_start_id:
            return ev.ts
    return None


def _compute_run_duration(unit_reviews: list[UnitReview]) -> float | None:
    """Aggregate duration across all units in the review."""
    if not unit_reviews:
        return None
    total = sum(u.duration_s or 0.0 for u in unit_reviews)
    return total or None


def _format_structural_offenders(offenders: object) -> str | None:
    """Return a compact report for Sentrux structural offender payloads."""
    if not isinstance(offenders, dict):
        return None
    normalized = {str(k): v for k, v in offenders.items()}
    sentrux_keys = {"complex_functions", "cog_complex_functions", "long_functions"}
    if sentrux_keys & normalized.keys():
        return format_structural_offender_report(normalized)
    return ", ".join(f"{key}: {value}" for key, value in normalized.items())


def _latest_run_completed_event(
    typed_events: list[_EventWithId],
    run_start_id: int,
) -> RunCompleted | None:
    latest: _EventWithId | None = None
    for ev in typed_events:
        if (
            isinstance(ev.event, RunCompleted)
            and ev.id > run_start_id
            and (latest is None or ev.id > latest.id)
        ):
            latest = ev
    return latest.event if latest is not None and isinstance(latest.event, RunCompleted) else None


def _run_completed_sentrux_payload(event: RunCompleted) -> dict[str, Any]:
    sentrux_raw = event.sentrux
    if isinstance(sentrux_raw, dict):
        return sentrux_raw
    if isinstance(sentrux_raw, str):
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(sentrux_raw)
            return payload if isinstance(payload, dict) else {}
    return {}


def _branch_verification_fields(sentrux: dict[str, Any]) -> dict[str, Any]:
    branch = sentrux.get("branch_verification")
    if not isinstance(branch, dict):
        return {
            "branch_verification_status": None,
            "branch_verification_error": None,
        }
    return {
        "branch_verification_status": branch.get("status"),
        "branch_verification_error": branch.get("error"),
    }


def _extract_run_completed_fields(
    typed_events: list[_EventWithId], run_start_id: int
) -> dict[str, Any]:
    """Extract run-level fields from the latest run_completed event after run_start_id.

    Returns a dict with keys: run_status, sentrux_degradation, sentrux_quality_before,
    sentrux_quality_after, sentrux_error, sentrux_offender_summary.
    """
    event = _latest_run_completed_event(typed_events, run_start_id)
    if event is None:
        return {}

    sentrux = _run_completed_sentrux_payload(event)
    offenders = sentrux.get("structural_offenders")
    offender_summary = _format_structural_offenders(offenders)

    return {
        "run_status": event.run_status,
        "sentrux_degradation": sentrux.get("degradation"),
        "sentrux_quality_before": sentrux.get("quality_before"),
        "sentrux_quality_after": sentrux.get("quality_after"),
        "sentrux_error": sentrux.get("error"),
        "sentrux_offender_summary": offender_summary,
        **_branch_verification_fields(sentrux),
    }


def _parse_runs_log_block(log_text: str, plan_name: str) -> dict[str, Any]:
    """Parse the latest matching block from .dgov/runs.log for run-level fields.

    Looks for a block starting with "[timestamp] plan_name ..." and extracts:
    - sentrux: "X -> Y" lines for quality before/after
    - sentrux_status: degradation detection
    - sentrux_error: error messages
    - sentrux_offenders: offender summary string
    """
    block_lines = _latest_runs_log_block(log_text.splitlines(), plan_name)
    if not block_lines:
        return {}

    result = _parse_runs_log_header(block_lines[0])
    for line in block_lines[1:]:
        result.update(_parse_runs_log_field(line))
    return result


def _latest_runs_log_block(lines: list[str], plan_name: str) -> list[str]:
    start = _latest_runs_log_block_start(lines, plan_name)
    if start is None:
        return []
    end = _runs_log_block_end(lines, start)
    return lines[start:end]


def _latest_runs_log_block_start(lines: list[str], plan_name: str) -> int | None:
    block_start: int | None = None
    for index, line in enumerate(lines):
        if _runs_log_header_plan(line) == plan_name:
            block_start = index
    return block_start


def _runs_log_header_plan(line: str) -> str | None:
    match = _RUNS_LOG_HEADER_RE.match(line)
    return match.group(2) if match else None


def _runs_log_block_end(lines: list[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        if _runs_log_header_plan(lines[index]) is not None:
            return index
    return len(lines)


def _parse_runs_log_header(line: str) -> dict[str, Any]:
    match = _RUNS_LOG_STATUS_RE.match(line)
    if not match:
        return {}
    status = _normalize_runs_log_status(match.group(1))
    return {"run_status": status} if status else {}


def _normalize_runs_log_status(status: str) -> str | None:
    normalized = _RUNS_LOG_STATUS_ALIASES.get(status, status)
    if normalized in _RUNS_LOG_RUN_STATUSES:
        return normalized
    return None


def _parse_runs_log_field(line: str) -> dict[str, Any]:
    result = _parse_sentrux_quality_line(line)
    if "sentrux_status: degradation" in line:
        result["sentrux_degradation"] = True
    for source_key, result_key in _RUNS_LOG_FIELD_KEYS.items():
        value = _parse_indented_runs_log_value(line, source_key)
        if value is not None:
            result[result_key] = value
    return result


def _parse_sentrux_quality_line(line: str) -> dict[str, Any]:
    match = _RUNS_LOG_SENTRUX_RE.search(line)
    if not match:
        return {}
    result: dict[str, Any] = {"sentrux_quality_before": int(match.group(1))}
    after_value = match.group(2)
    if after_value != "None":
        result["sentrux_quality_after"] = int(after_value)
    return result


def _parse_indented_runs_log_value(line: str, key: str) -> str | None:
    match = re.match(rf"\s+{re.escape(key)}:\s*(.+)", line)
    return match.group(1).strip() if match else None


def _load_runs_log_fields(project_root: str, plan_name: str) -> dict[str, Any]:
    """Load and parse .dgov/runs.log for run-level fields. Returns empty dict on missing file."""
    log_path = Path(project_root) / ".dgov" / "runs.log"
    if not log_path.exists():
        return {}
    try:
        log_text = log_path.read_text()
        return _parse_runs_log_block(log_text, plan_name)
    except (OSError, UnicodeDecodeError):
        return {}


def _unknown_plan_review(plan_dir: Path | None) -> PlanReview:
    return PlanReview(
        plan_name="(unknown)",
        source_dir=plan_dir,
        last_run_ts=None,
        last_run_duration_s=None,
    )


def _filtered_plan_units(compiled_path: Path, only: str | None) -> dict[str, dict]:
    tasks = _load_plan_units(compiled_path)
    if only is None:
        return tasks
    return {uid: data for uid, data in tasks.items() if uid == only}


def _events_after_run_start(plan_events: list[dict], run_start_id: int) -> list[dict]:
    return [ev for ev in plan_events if ev.get("id", 0) > run_start_id]


def _unit_lifecycle_events(scoped_plan_events: list[dict], unit_id: str) -> list[dict]:
    return [
        ev
        for ev in scoped_plan_events
        if ev.get("task_slug") == unit_id and ev.get("event") != "worker_log"
    ]


def _build_unit_reviews_for_plan(
    *,
    project_root: str,
    plan_name: str,
    tasks: dict[str, dict],
    scoped_plan_events: list[dict],
    run_start_id: int,
    include_full_diff: bool,
    iteration_budget: int | None,
) -> list[UnitReview]:
    deploy_records = {r.unit: r for r in read_deploy_log(project_root, plan_name)}
    unit_reviews: list[UnitReview] = []
    for unit_id in sorted(tasks):
        lifecycle = _unit_lifecycle_events(scoped_plan_events, unit_id)
        worker_events = _fetch_worker_events_for_unit(
            project_root, plan_name, unit_id, lifecycle, run_start_id
        )
        unit_reviews.append(
            _build_unit_review(
                unit_id=unit_id,
                task_data=tasks[unit_id],
                deploy_record=deploy_records.get(unit_id),
                unit_events=_combine_unit_events(lifecycle, worker_events),
                project_root=project_root,
                include_full_diff=include_full_diff,
                iteration_budget=iteration_budget,
            )
        )
    return unit_reviews


def _load_review_run_fields(
    project_root: str,
    plan_name: str,
    typed_plan_events: list[_EventWithId],
    run_start_id: int,
) -> dict[str, Any]:
    run_fields = _extract_run_completed_fields(typed_plan_events, run_start_id)
    if run_fields:
        return run_fields
    return _load_runs_log_fields(project_root, plan_name)


def _plan_review_from_fields(
    *,
    plan_name: str,
    plan_dir: Path | None,
    last_run_ts: str | None,
    run_duration: float | None,
    unit_reviews: list[UnitReview],
    run_fields: dict[str, Any],
) -> PlanReview:
    return PlanReview(
        plan_name=plan_name,
        source_dir=plan_dir,
        last_run_ts=last_run_ts,
        last_run_duration_s=run_duration,
        units=unit_reviews,
        run_status=run_fields.get("run_status"),
        sentrux_degradation=run_fields.get("sentrux_degradation"),
        sentrux_quality_before=run_fields.get("sentrux_quality_before"),
        sentrux_quality_after=run_fields.get("sentrux_quality_after"),
        sentrux_error=run_fields.get("sentrux_error"),
        sentrux_offender_summary=run_fields.get("sentrux_offender_summary"),
        branch_verification_status=run_fields.get("branch_verification_status"),
        branch_verification_error=run_fields.get("branch_verification_error"),
    )


def load_review(
    project_root: str,
    compiled_path: Path,
    plan_dir: Path | None = None,
    only: str | None = None,
    include_full_diff: bool = False,
    iteration_budget: int | None = None,
) -> PlanReview:
    """Build a PlanReview for the latest run of a plan.

    `compiled_path` points at `_compiled.toml`. `plan_dir` is optional
    metadata for the formatter (so it can show "source: .dgov/plans/X").
    `only` restricts to a single exact-match unit id. `include_full_diff`
    pulls full git patches for deployed units (more expensive — opt in).
    """
    plan_name = _plan_name_from_compiled(compiled_path)
    if plan_name is None:
        return _unknown_plan_review(plan_dir)

    tasks = _filtered_plan_units(compiled_path, only)
    plan_events = read_events(project_root, plan_name=plan_name)
    typed_plan_events = _convert_events(plan_events)
    run_start_id = _find_run_start_id(typed_plan_events, plan_name)
    unit_reviews = _build_unit_reviews_for_plan(
        project_root=project_root,
        plan_name=plan_name,
        tasks=tasks,
        scoped_plan_events=_events_after_run_start(plan_events, run_start_id),
        run_start_id=run_start_id,
        include_full_diff=include_full_diff,
        iteration_budget=iteration_budget,
    )
    last_run_ts = _extract_run_start_ts(typed_plan_events, run_start_id)
    run_duration = _compute_run_duration(unit_reviews)
    run_fields = _load_review_run_fields(project_root, plan_name, typed_plan_events, run_start_id)
    return _plan_review_from_fields(
        plan_name=plan_name,
        plan_dir=plan_dir,
        last_run_ts=last_run_ts,
        run_duration=run_duration,
        unit_reviews=unit_reviews,
        run_fields=run_fields,
    )


def load_run_envelope(project_root: str, compiled_path: Path) -> RunEnvelope:
    """Load run-level status without the per-unit review cost."""
    plan_name = _plan_name_from_compiled(compiled_path)
    if plan_name is None:
        return RunEnvelope(plan_name="(unknown)", last_run_ts=None)

    plan_events = read_events(project_root, plan_name=plan_name)
    typed_plan_events = _convert_events(plan_events)
    run_start_id = _find_run_start_id(typed_plan_events, plan_name)
    run_fields = _extract_run_completed_fields(typed_plan_events, run_start_id)
    if not run_fields:
        run_fields = _load_runs_log_fields(project_root, plan_name)

    return RunEnvelope(
        plan_name=plan_name,
        last_run_ts=_extract_run_start_ts(typed_plan_events, run_start_id),
        run_status=run_fields.get("run_status"),
        sentrux_degradation=run_fields.get("sentrux_degradation"),
        sentrux_quality_before=run_fields.get("sentrux_quality_before"),
        sentrux_quality_after=run_fields.get("sentrux_quality_after"),
        sentrux_error=run_fields.get("sentrux_error"),
        sentrux_offender_summary=run_fields.get("sentrux_offender_summary"),
        branch_verification_status=run_fields.get("branch_verification_status"),
        branch_verification_error=run_fields.get("branch_verification_error"),
    )
