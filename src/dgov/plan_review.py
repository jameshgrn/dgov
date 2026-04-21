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
from typing import Literal

from dgov.deploy_log import read as read_deploy_log
from dgov.persistence import read_events

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


def _extract_path_mentions(text: str) -> tuple[str, ...]:
    """Return ordered unique file-like path mentions from free text."""
    seen: set[str] = set()
    paths: list[str] = []
    for raw in _PATH_TOKEN_SPLIT_RE.split(text):
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

    # Status reflects the CURRENT run's outcome, not any historical deploy.
    # A unit that merged in an earlier run but failed in this run is "failed".
    # A unit in flight during the current run is "active".
    # A unit that didn't run at all in this run falls back to deploy_log.
    status: UnitStatus
    if rollup["ran_in_run"]:
        if rollup["merged_in_run"]:
            status = "deployed"
        elif rollup["failed_in_run"] or rollup["error"] or rollup["reject_verdict"]:
            status = "failed"
        else:
            status = "active"
    elif deploy_record is not None:
        # Stale deploy from a prior run; this run did not touch the unit.
        status = "deployed"
    else:
        status = "not_run"

    commit_sha: str | None = None
    commit_message: str | None = None
    commit_ts: str | None = None
    diff_stat: DiffStat | None = None
    landed_files: tuple[str, ...] = ()
    full_diff: str | None = None
    worker_note_mismatches: tuple[str, ...] = ()

    if status == "deployed" and deploy_record is not None:
        commit_sha = deploy_record.sha
        commit_ts = deploy_record.ts
        commit_message = _git_show_message(project_root, commit_sha)
        diff_stat = _git_show_stat(project_root, commit_sha)
        landed_files = _git_show_paths(project_root, commit_sha) or ()
        worker_note_mismatches = _worker_note_mismatches(rollup["done_summary"], landed_files)
        if include_full_diff:
            full_diff = _git_show_full_diff(project_root, commit_sha)

    attempts = 1 + rollup["settlement_retries"] if unit_events else 0
    settlement = _settlement_result(status, rollup["settlement_retries"], rollup["reject_verdict"])

    last_thought = rollup["thoughts"][-1] if rollup["thoughts"] else None
    hint = None
    if status == "failed":
        unit_iteration_budget = task_data.get("iteration_budget", iteration_budget)
        hint = synthesize_hint(
            rollup["reject_verdict"],
            rollup["error"],
            rollup["iterations"],
            unit_iteration_budget,
        )

    # Only count self-corrections on units that made it to deployed — a
    # failed unit's failed tool calls were not recovered from.
    self_corrections = rollup["failed_tool_calls"] if status == "deployed" else 0

    return UnitReview(
        unit=unit_id,
        summary=task_data.get("summary", ""),
        status=status,
        agent=task_data.get("agent", ""),
        commit_sha=commit_sha,
        commit_message=commit_message,
        commit_ts=commit_ts,
        diff_stat=diff_stat,
        landed_files=landed_files,
        full_diff=full_diff,
        duration_s=rollup["duration_s"],
        iterations=rollup["iterations"],
        attempts=attempts,
        settlement=settlement,
        done_summary=rollup["done_summary"],
        worker_note_mismatches=worker_note_mismatches,
        thoughts=tuple(rollup["thoughts"]),
        activity=tuple(rollup["activity"]),
        self_corrections=self_corrections,
        reject_verdict=rollup["reject_verdict"],
        error=rollup["error"],
        last_thought=last_thought if status in ("failed", "active") else None,
        hint=hint,
        integration_risk_level=rollup["integration_risk_level"],
        integration_risk_detected=rollup["integration_risk_detected"],
        integration_candidate_passed=rollup["integration_candidate_passed"],
        integration_failure_class=rollup["integration_failure_class"],
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

    # Per-unit worker_log events — scoped by task_slug + run_start_id.
    def _events_for_unit(uid: str) -> list[dict]:
        lifecycle = [
            ev
            for ev in scoped_plan_events
            if ev.get("task_slug") == uid and ev.get("event") != "worker_log"
        ]
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
        if not worker_events:
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
            worker_events = fallback_events
        # Interleave lifecycle (from plan-scoped fetch) and worker logs chronologically by id.
        combined = worker_events + lifecycle
        combined.sort(key=lambda e: e.get("id", 0))
        return combined

    deploy_records = {r.unit: r for r in read_deploy_log(project_root, plan_name)}

    unit_reviews: list[UnitReview] = []
    for uid in sorted(tasks):
        unit_reviews.append(
            _build_unit_review(
                unit_id=uid,
                task_data=tasks[uid],
                deploy_record=deploy_records.get(uid),
                unit_events=_events_for_unit(uid),
                project_root=project_root,
                include_full_diff=include_full_diff,
                iteration_budget=iteration_budget,
            )
        )

    # Last-run envelope: look at the run_start ts and aggregate unit durations.
    last_run_ts: str | None = None
    for ev in plan_events:
        if ev.get("event") == "run_start" and ev.get("id", 0) == run_start_id:
            last_run_ts = ev.get("ts")
            break
    run_duration = sum(u.duration_s or 0.0 for u in unit_reviews) or None if unit_reviews else None

    return PlanReview(
        plan_name=plan_name,
        source_dir=plan_dir,
        last_run_ts=last_run_ts,
        last_run_duration_s=run_duration,
        units=unit_reviews,
    )
