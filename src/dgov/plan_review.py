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

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dgov.deploy_log import read as read_deploy_log
from dgov.persistence import read_events

UnitStatus = Literal["deployed", "failed", "pending", "not_run"]
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
    full_diff: str | None = None  # Populated only when caller asks for it
    # Execution info (populated for any unit that actually ran)
    duration_s: float | None = None
    iterations: int | None = None
    attempts: int = 0
    settlement: SettlementResult = "n/a"
    done_summary: str | None = None
    thoughts: tuple[str, ...] = ()  # All worker thoughts in order
    activity: tuple[dict, ...] = ()  # All tool calls in order
    # Failure info (populated for status == "failed")
    reject_verdict: str | None = None
    error: str | None = None
    last_thought: str | None = None
    hint: str | None = None


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
    """Handle worker_log events: thoughts, calls, done, error."""
    log_type = ev.get("log_type")
    content = ev.get("content")
    if log_type == "thought" and isinstance(content, str):
        state["thoughts"].append(content)
    elif log_type == "call" and isinstance(content, dict):
        state["iterations"] = state["iterations"] + 1
        state["activity"].append(content)
    elif log_type == "done" and isinstance(content, str):
        state["done_summary"] = content
    elif log_type == "error" and isinstance(content, str) and state["error"] is None:
        state["error"] = content


def _rollup_unit_events(unit_events: list[dict]) -> dict:
    """Collapse a unit's events into a rollup dict used by _build_unit_review."""
    state: dict = {
        "thoughts": [],
        "activity": [],
        "iterations": 0,
        "done_summary": None,
        "error": None,
        "reject_verdict": None,
        "settlement_retries": 0,
        "dispatched_ts": None,
        "terminal_ts": None,
        "merge_sha": None,
        "merged_in_run": False,
        "failed_in_run": False,
    }

    for ev in unit_events:
        if ev.get("event") == "worker_log":
            _apply_worker_log_event(ev, state)
        else:
            _apply_lifecycle_event(ev, state)

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
        "done_summary": state["done_summary"],
        "error": state["error"],
        "reject_verdict": state["reject_verdict"],
        "settlement_retries": state["settlement_retries"],
        "duration_s": duration_s,
        "merge_sha": state["merge_sha"],
        "merged_in_run": state["merged_in_run"],
        "failed_in_run": state["failed_in_run"],
        "ran_in_run": ran_in_run,
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
    # A unit that didn't run at all in this run falls back to deploy_log.
    status: UnitStatus
    if rollup["ran_in_run"]:
        if rollup["merged_in_run"]:
            status = "deployed"
        elif rollup["failed_in_run"] or rollup["error"] or rollup["reject_verdict"]:
            status = "failed"
        else:
            # Dispatched but no terminal event yet — still in flight or the
            # run was killed mid-task. Surface as failed so the user notices.
            status = "failed"
    elif deploy_record is not None:
        # Stale deploy from a prior run; this run did not touch the unit.
        status = "deployed"
    else:
        status = "not_run"

    commit_sha: str | None = None
    commit_message: str | None = None
    commit_ts: str | None = None
    diff_stat: DiffStat | None = None
    full_diff: str | None = None

    if status == "deployed" and deploy_record is not None:
        commit_sha = deploy_record.sha
        commit_ts = deploy_record.ts
        commit_message = _git_show_message(project_root, commit_sha)
        diff_stat = _git_show_stat(project_root, commit_sha)
        if include_full_diff:
            full_diff = _git_show_full_diff(project_root, commit_sha)

    attempts = 1 + rollup["settlement_retries"] if unit_events else 0
    settlement = _settlement_result(status, rollup["settlement_retries"], rollup["reject_verdict"])

    last_thought = rollup["thoughts"][-1] if rollup["thoughts"] else None
    hint = None
    if status == "failed":
        hint = synthesize_hint(
            rollup["reject_verdict"],
            rollup["error"],
            rollup["iterations"],
            iteration_budget,
        )

    return UnitReview(
        unit=unit_id,
        summary=task_data.get("summary", ""),
        status=status,
        agent=task_data.get("agent", ""),
        commit_sha=commit_sha,
        commit_message=commit_message,
        commit_ts=commit_ts,
        diff_stat=diff_stat,
        full_diff=full_diff,
        duration_s=rollup["duration_s"],
        iterations=rollup["iterations"],
        attempts=attempts,
        settlement=settlement,
        done_summary=rollup["done_summary"],
        thoughts=tuple(rollup["thoughts"]),
        activity=tuple(rollup["activity"]),
        reject_verdict=rollup["reject_verdict"],
        error=rollup["error"],
        last_thought=last_thought if status == "failed" else None,
        hint=hint,
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
        worker_events = read_events(project_root, task_slug=uid, after_id=run_start_id)
        # Interleave lifecycle (from plan-scoped fetch) and worker logs chronologically by id.
        lifecycle = [ev for ev in scoped_plan_events if ev.get("task_slug") == uid]
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
