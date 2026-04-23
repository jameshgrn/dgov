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

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dgov.deploy_log import read as read_deploy_log
from dgov.persistence import read_events
from dgov.repo_snapshot import format_structural_offender_report

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
class UnitReview:
    """Per-unit review snapshot."""

    unit: str
    summary: str
    status: UnitStatus
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
    iterations: int | None = None
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
    if iterations is not None and iteration_budget and iterations >= iteration_budget:
        return (
            f"worker hit the {iteration_budget}-iteration budget — task is probably too "
            "large; split it or clarify the Edit section"
        )

    if verdict is None:
        return None

    v = verdict.lower()
    if v == "scope_violation":
        # Error text often contains the offending path(s); surface a concrete action.
        if error and ":" in error:
            return (
                "worker touched unclaimed files — add them to files.edit OR split into a new task"
            )
        return "worker touched unclaimed files — add them to files.edit"
    if v == "empty_diff":
        return (
            "worker produced no changes — Orient/Edit/Verify is probably unclear, "
            "or the edit target is already in the desired state"
        )
    if v in ("lint_fail", "format_fail"):
        return "autofix couldn't fix — lint/format failure needs manual intervention"
    if v == "test_fail":
        return "tests failed after the edit — check Verify commands against the plan"
    if v == "review_hook_fail":
        return "a project review_hook rejected the commit — see error for which hook"
    return None


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


def _find_run_start_id(events: list[dict], plan_name: str) -> int:
    """Return the id of the latest run_start event for this plan, or 0."""
    latest = 0
    for ev in events:
        is_match = (
            ev.get("event") == "run_start"
            and ev.get("plan_name") == plan_name
            and ev.get("id", 0) > latest
        )
        if is_match:
            latest = int(ev["id"])
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
    tokens = [t.strip() for t in _PATH_TOKEN_SPLIT_RE.split(text) if t.strip()]

    for i, raw in enumerate(tokens):
        path = raw.strip().strip(".").strip("*_`<>")
        if path.startswith("./"):
            path = path[2:]
        if not path or "." not in path:
            continue
        suffix = path.rsplit(".", 1)[1].lower()
        if "/" not in path and suffix not in _ROOT_FILE_SUFFIXES:
            continue
        if path in seen:
            continue

        # Check for change-claim context in the preceding window.
        start_idx = max(0, i - _CHANGE_CONTEXT_WINDOW)
        preceding_tokens = tokens[start_idx:i]

        # Check if any token is a change verb (case-insensitive).
        has_change_verb = any(t.lower() in _CHANGE_VERBS for t in preceding_tokens)

        # Check if the immediate context suggests non-change usage.
        # If the token right before the path is a non-change word, suppress the change claim.
        has_non_change_context = False
        if preceding_tokens:
            # Check the token immediately before the path.
            immediate_prev = preceding_tokens[-1].lower()
            if immediate_prev in _NON_CHANGE_CONTEXT_WORDS:
                has_non_change_context = True

        # Only include the path if it has a change verb and no non-change context override.
        if has_change_verb and not has_non_change_context:
            seen.add(path)
            paths.append(path)

    return tuple(paths)


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

# Events terminal to this run's task lifecycle. review_fail is included
# because a review rejection prevents merge and is the last thing we'll
# see for that task, even though task_merge_failed is the "proper"
# terminal. task_timed_out is also terminal.
_TERMINAL_EVENTS = {
    "merge_completed",
    "task_merge_failed",
    "review_fail",
    "task_timed_out",
}


def _maybe_extract_merge_sha(ev: dict, state: dict) -> None:
    """Extract merge_sha from event if present and valid."""
    sha = ev.get("merge_sha")
    if isinstance(sha, str):
        state["merge_sha"] = sha


def _extract_review_fail_fields(ev: dict, state: dict) -> None:
    """Extract verdict and error from review_fail event."""
    state["reject_verdict"] = ev.get("verdict") or state["reject_verdict"]
    err = ev.get("error")
    if isinstance(err, str):
        state["error"] = err


def _apply_terminal_event(event_type: str, ev: dict, state: dict) -> None:
    """Apply terminal event effects to state."""
    state["terminal_ts"] = ev.get("ts")
    if event_type == "merge_completed":
        state["merged_in_run"] = True
        _maybe_extract_merge_sha(ev, state)
    elif event_type in ("task_merge_failed", "review_fail", "task_timed_out"):
        state["failed_in_run"] = True
        if event_type == "task_merge_failed":
            _maybe_extract_merge_sha(ev, state)
        elif event_type == "review_fail":
            _extract_review_fail_fields(ev, state)


def _apply_lifecycle_event(ev: dict, state: dict) -> None:
    """Handle lifecycle events: dispatch, terminal, settlement_retry, review_fail."""
    event_type = ev.get("event")
    if event_type == "dag_task_dispatched" and state["dispatched_ts"] is None:
        state["dispatched_ts"] = ev.get("ts")
        return
    if event_type in _TERMINAL_EVENTS:
        _apply_terminal_event(event_type, ev, state)
        return
    if event_type == "settlement_retry":
        state["settlement_retries"] = state["settlement_retries"] + 1
        # settlement_retry resets merged-in-run until a later merge_completed.
        state["merged_in_run"] = False
        state["failed_in_run"] = False
        return
    if event_type == "iteration_fork":
        depth = ev.get("fork_depth")
        if isinstance(depth, int):
            state["fork_depth"] = max(state["fork_depth"], depth)
        return
    if event_type == "self_review_passed":
        state["self_review_outcome"] = "passed"
    elif event_type == "self_review_rejected":
        state["self_review_outcome"] = "rejected"
    elif event_type == "self_review_auto_passed":
        state["self_review_outcome"] = "auto_passed"
    elif event_type == "self_review_error":
        state["self_review_outcome"] = "error"


def _apply_worker_log_event(ev: dict, state: dict) -> None:
    """Handle worker_log events: thoughts, calls, results, done, error."""
    log_type = ev.get("log_type")
    content = ev.get("content")
    if log_type == "thought" and isinstance(content, str):
        state["thoughts"].append(content)
    elif log_type == "call" and isinstance(content, dict):
        state["iterations"] = state["iterations"] + 1
        state["activity"].append(content)
    elif log_type == "result" and isinstance(content, dict):
        if content.get("status") == "failed":
            state["failed_tool_calls"] = state["failed_tool_calls"] + 1
    elif log_type == "done" and isinstance(content, str):
        state["done_summary"] = content
    elif log_type == "error" and isinstance(content, str) and state["error"] is None:
        state["error"] = content


def _apply_semantic_settlement_event(ev: dict, state: dict) -> None:
    """Handle semantic settlement events: risk scoring, candidate validation, gates."""
    event_type = ev.get("event")
    if event_type == "integration_risk_scored":
        # Capture risk level and overlap detection
        risk_level = ev.get("risk_level")
        if isinstance(risk_level, str):
            state["integration_risk_level"] = risk_level
        # python_overlap_detected is boolean in payload
        if ev.get("python_overlap_detected") is True:
            state["integration_risk_detected"] = True
        # Also check for any overlap_evidence in the payload
        overlap_evidence = ev.get("overlap_evidence")
        if isinstance(overlap_evidence, list) and len(overlap_evidence) > 0:
            state["integration_risk_detected"] = True
    elif event_type == "integration_candidate_passed":
        state["integration_candidate_passed"] = True
    elif event_type == "integration_candidate_failed" or event_type == "semantic_gate_rejected":
        state["integration_candidate_passed"] = False
        fc = ev.get("failure_class")
        if isinstance(fc, str):
            state["integration_failure_class"] = fc


def _rollup_unit_events(unit_events: list[dict]) -> dict:
    """Collapse a unit's events into a rollup dict used by _build_unit_review."""
    state: dict = {
        "thoughts": [],
        "activity": [],
        "iterations": 0,
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
    }

    for ev in unit_events:
        if ev.get("event") == "worker_log":
            _apply_worker_log_event(ev, state)
        else:
            _apply_lifecycle_event(ev, state)
            _apply_semantic_settlement_event(ev, state)

    duration_s: float | None = None
    dispatched_ts = state["dispatched_ts"]
    terminal_ts = state["terminal_ts"]
    if dispatched_ts and terminal_ts:
        start = _iso_to_epoch(dispatched_ts)
        end = _iso_to_epoch(terminal_ts)
        if start is not None and end is not None:
            duration_s = max(0.0, end - start)

    # "ran_in_run" is True if the unit had any task activity in the current
    # run window (dispatched, called tools, or reached a terminal event).
    # Used by _build_unit_review to distinguish current-run outcomes from
    # stale deploy-log records.
    ran_in_run = bool(unit_events)
    iterations = state["iterations"]

    return {
        "thoughts": state["thoughts"],
        "activity": state["activity"],
        "iterations": iterations if iterations > 0 else None,
        "failed_tool_calls": state["failed_tool_calls"],
        "done_summary": state["done_summary"],
        "error": state["error"],
        "reject_verdict": state["reject_verdict"],
        "settlement_retries": state["settlement_retries"],
        "duration_s": duration_s,
        "merge_sha": state["merge_sha"],
        "merged_in_run": state["merged_in_run"],
        "failed_in_run": state["failed_in_run"],
        "ran_in_run": ran_in_run,
        # Fork and self-review telemetry
        "fork_depth": state["fork_depth"],
        "self_review_outcome": state["self_review_outcome"],
        # Integration risk telemetry
        "integration_risk_level": state["integration_risk_level"],
        "integration_risk_detected": state["integration_risk_detected"],
        "integration_candidate_passed": state["integration_candidate_passed"],
        "integration_failure_class": state["integration_failure_class"],
    }


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


def _build_unit_review(
    unit_id: str,
    task_data: dict,
    deploy_record,
    unit_events: list[dict],
    project_root: str,
    include_full_diff: bool,
    iteration_budget: int | None,
) -> UnitReview:
    rollup = _rollup_unit_events(unit_events)

    # Determine unit status from rollup state and historical deploy record
    status = _compute_unit_status(
        ran_in_run=rollup["ran_in_run"],
        merged_in_run=rollup["merged_in_run"],
        failed_in_run=rollup["failed_in_run"],
        has_error=bool(rollup["error"]),
        has_reject_verdict=bool(rollup["reject_verdict"]),
        deploy_record=deploy_record,
    )

    # Fetch commit info for deployed units, or use empty placeholders
    if status == "deployed" and deploy_record is not None:
        commit_info = _fetch_deployed_commit_info(
            project_root, deploy_record, rollup["done_summary"], include_full_diff
        )
    else:
        commit_info = _empty_commit_info()

    # Compute derived metrics
    attempts = _compute_attempts(unit_events, rollup["settlement_retries"])
    settlement = _settlement_result(status, rollup["settlement_retries"], rollup["reject_verdict"])
    self_corrections = _compute_self_corrections(rollup["failed_tool_calls"], status)
    last_thought = _compute_last_thought(rollup["thoughts"], status)

    # Synthesize hint only for failed units
    hint = None
    if status == "failed":
        unit_iteration_budget = task_data.get("iteration_budget", iteration_budget)
        hint = synthesize_hint(
            rollup["reject_verdict"],
            rollup["error"],
            rollup["iterations"],
            unit_iteration_budget,
        )

    return UnitReview(
        unit=unit_id,
        summary=task_data.get("summary", ""),
        status=status,
        agent=task_data.get("agent", ""),
        commit_sha=commit_info["commit_sha"],
        commit_message=commit_info["commit_message"],
        commit_ts=commit_info["commit_ts"],
        diff_stat=commit_info["diff_stat"],
        landed_files=commit_info["landed_files"],
        full_diff=commit_info["full_diff"],
        duration_s=rollup["duration_s"],
        iterations=rollup["iterations"],
        attempts=attempts,
        settlement=settlement,
        done_summary=rollup["done_summary"],
        worker_note_mismatches=commit_info["worker_note_mismatches"],
        thoughts=tuple(rollup["thoughts"]),
        activity=tuple(rollup["activity"]),
        self_corrections=self_corrections,
        fork_depth=rollup["fork_depth"],
        self_review_outcome=rollup["self_review_outcome"],
        reject_verdict=rollup["reject_verdict"],
        error=rollup["error"],
        last_thought=last_thought,
        hint=hint,
        integration_risk_level=rollup["integration_risk_level"],
        integration_risk_detected=rollup["integration_risk_detected"],
        integration_candidate_passed=rollup["integration_candidate_passed"],
        integration_failure_class=rollup["integration_failure_class"],
    )


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
    worker_events = [
        ev
        for ev in read_events(
            project_root,
            plan_name=plan_name,
            task_slug=uid,
            after_id=run_start_id,
        )
        if ev.get("event") == "worker_log"
    ]
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


def _extract_run_start_ts(plan_events: list[dict], run_start_id: int) -> str | None:
    """Extract timestamp of the run_start event matching the given id."""
    for ev in plan_events:
        if ev.get("event") == "run_start" and ev.get("id", 0) == run_start_id:
            return ev.get("ts")
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


def _extract_run_completed_fields(plan_events: list[dict], run_start_id: int) -> dict[str, Any]:
    """Extract run-level fields from the latest run_completed event after run_start_id.

    Returns a dict with keys: run_status, sentrux_degradation, sentrux_quality_before,
    sentrux_quality_after, sentrux_error, sentrux_offender_summary.
    """
    # Find the latest run_completed event with id > run_start_id
    latest_run_completed: dict | None = None
    for ev in plan_events:
        is_later_run_completed = (
            ev.get("event") == "run_completed"
            and ev.get("id", 0) > run_start_id
            and (
                latest_run_completed is None or ev.get("id", 0) > latest_run_completed.get("id", 0)
            )
        )
        if is_later_run_completed:
            latest_run_completed = ev

    if latest_run_completed is None:
        return {}

    sentrux = latest_run_completed.get("sentrux") or {}
    offenders = sentrux.get("structural_offenders") if isinstance(sentrux, dict) else None
    offender_summary = _format_structural_offenders(offenders)

    return {
        "run_status": latest_run_completed.get("run_status"),
        "sentrux_degradation": sentrux.get("degradation") if isinstance(sentrux, dict) else None,
        "sentrux_quality_before": sentrux.get("quality_before")
        if isinstance(sentrux, dict)
        else None,
        "sentrux_quality_after": sentrux.get("quality_after")
        if isinstance(sentrux, dict)
        else None,
        "sentrux_error": sentrux.get("error") if isinstance(sentrux, dict) else None,
        "sentrux_offender_summary": offender_summary,
    }


def _parse_runs_log_block(log_text: str, plan_name: str) -> dict[str, Any]:
    """Parse the latest matching block from .dgov/runs.log for run-level fields.

    Looks for a block starting with "[timestamp] plan_name ..." and extracts:
    - sentrux: "X -> Y" lines for quality before/after
    - sentrux_status: degradation detection
    - sentrux_error: error messages
    - sentrux_offenders: offender summary string
    """
    lines = log_text.splitlines()
    # Find the latest block header matching this plan name
    block_start = -1
    for i, line in enumerate(lines):
        match = re.match(r"^\[([^\]]+)\]\s+(\S+)", line)
        if match and match.group(2) == plan_name:
            block_start = i

    if block_start == -1:
        return {}

    # Collect all lines in this block (until next block or EOF)
    block_lines: list[str] = []
    for i in range(block_start, len(lines)):
        line = lines[i]
        # Next block starts with timestamp in brackets at line start
        if i > block_start and re.match(r"^\[([^\]]+)\]\s+(\S+)", line):
            break
        block_lines.append(line)

    result: dict[str, object] = {}
    offenders_str: str | None = None

    for line in block_lines:
        # Parse "sentrux: X -> Y" for quality values
        sentrux_match = re.search(r"sentrux:\s*(\d+)\s*->\s*(\d+|None)", line)
        if sentrux_match:
            try:
                result["sentrux_quality_before"] = int(sentrux_match.group(1))
                after_str = sentrux_match.group(2)
                if after_str != "None":
                    result["sentrux_quality_after"] = int(after_str)
            except (ValueError, TypeError):
                pass

        # Parse "sentrux_status: degradation"
        if "sentrux_status: degradation" in line:
            result["sentrux_degradation"] = True

        # Parse "sentrux_error: ..."
        error_match = re.match(r"\s+sentrux_error:\s*(.+)", line)
        if error_match:
            result["sentrux_error"] = error_match.group(1).strip()

        # Parse "sentrux_offenders: ..."
        offenders_match = re.match(r"\s+sentrux_offenders:\s*(.+)", line)
        if offenders_match:
            offenders_str = offenders_match.group(1).strip()

    if offenders_str:
        result["sentrux_offender_summary"] = offenders_str

    return result


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
        return PlanReview(
            plan_name="(unknown)",
            source_dir=plan_dir,
            last_run_ts=None,
            last_run_duration_s=None,
        )

    tasks = _load_plan_units(compiled_path)
    if only is not None:
        tasks = {uid: data for uid, data in tasks.items() if uid == only}

    # Pull all events for this plan in one shot, then split per unit in-memory.
    # Worker_log events do not carry plan_name, so we fetch those per task_slug.
    plan_events = read_events(project_root, plan_name=plan_name)
    run_start_id = _find_run_start_id(plan_events, plan_name)

    # Lifecycle events scoped to this run only.
    scoped_plan_events = [ev for ev in plan_events if ev.get("id", 0) > run_start_id]

    deploy_records = {r.unit: r for r in read_deploy_log(project_root, plan_name)}

    unit_reviews: list[UnitReview] = []
    for uid in sorted(tasks):
        # Build lifecycle events for this unit from plan-scoped events.
        lifecycle = [
            ev
            for ev in scoped_plan_events
            if ev.get("task_slug") == uid and ev.get("event") != "worker_log"
        ]
        # Fetch and combine with worker log events.
        worker_events = _fetch_worker_events_for_unit(
            project_root, plan_name, uid, lifecycle, run_start_id
        )
        unit_events = _combine_unit_events(lifecycle, worker_events)

        unit_reviews.append(
            _build_unit_review(
                unit_id=uid,
                task_data=tasks[uid],
                deploy_record=deploy_records.get(uid),
                unit_events=unit_events,
                project_root=project_root,
                include_full_diff=include_full_diff,
                iteration_budget=iteration_budget,
            )
        )

    # Last-run envelope: extract timestamp and aggregate unit durations.
    last_run_ts = _extract_run_start_ts(plan_events, run_start_id)
    run_duration = _compute_run_duration(unit_reviews)

    # Extract run-level fields from structured events (preferred) or runs.log fallback
    run_fields = _extract_run_completed_fields(plan_events, run_start_id)
    if not run_fields:
        run_fields = _load_runs_log_fields(project_root, plan_name)

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
    )
