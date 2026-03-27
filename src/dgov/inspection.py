"""Pane inspection: review, diff, rebase."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path

from dgov.persistence import (
    PROTECTED_FILES,
    emit_event,
    get_pane,
    read_events,
)
from dgov.status import _compute_freshness

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    conflicts: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReviewTests:
    passed: bool | None = None
    ran: list[str] = field(default_factory=list)
    output: str = ""
    no_tests_found: bool = False
    timed_out: bool = False


@dataclass
class ReviewFreshness:
    status: str | None = None
    commits_since_base: int = 0
    overlapping_files: list[str] = field(default_factory=list)
    pane_age_hours: float = 0.0
    stale_files: list[str] = field(default_factory=list)


@dataclass
class ReviewAutomation:
    retry_count: int = 0
    auto_responses: int = 0
    lt_gov: bool = False


@dataclass
class ReviewContract:
    claim_violations: list[str] = field(default_factory=list)
    missing_test_coverage: list[str] = field(default_factory=list)
    evals: list[dict] | None = None


@dataclass
class ReviewInfo:
    slug: str
    verdict: str = "unknown"
    commit_count: int = 0
    branch: str = ""
    stat: str = ""
    diff: str = ""
    commit_log: str = ""
    uncommitted: bool = False
    files_changed: int = 0
    changed_files: list[str] = field(default_factory=list)
    protected_touched: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    error: str | None = None
    tests: ReviewTests = field(default_factory=ReviewTests)
    freshness_info: ReviewFreshness = field(default_factory=ReviewFreshness)
    automation: ReviewAutomation = field(default_factory=ReviewAutomation)
    contract: ReviewContract = field(default_factory=ReviewContract)
    tests_passed_init: InitVar[bool | None] = None
    tests_ran_init: InitVar[list[str] | None] = None
    test_output_init: InitVar[str] = ""
    no_tests_found_init: InitVar[bool] = False
    timed_out_init: InitVar[bool] = False
    freshness_init: InitVar[str | None] = None
    commits_since_base_init: InitVar[int] = 0
    overlapping_files_init: InitVar[list[str] | None] = None
    pane_age_hours_init: InitVar[float] = 0.0
    stale_files_init: InitVar[list[str] | None] = None
    retry_count_init: InitVar[int] = 0
    auto_responses_init: InitVar[int] = 0
    lt_gov_init: InitVar[bool] = False
    claim_violations_init: InitVar[list[str] | None] = None
    missing_test_coverage_init: InitVar[list[str] | None] = None
    evals_init: InitVar[list[dict] | None] = None

    def __post_init__(
        self,
        tests_passed_init: bool | None,
        tests_ran_init: list[str] | None,
        test_output_init: str,
        no_tests_found_init: bool,
        timed_out_init: bool,
        freshness_init: str | None,
        commits_since_base_init: int,
        overlapping_files_init: list[str] | None,
        pane_age_hours_init: float,
        stale_files_init: list[str] | None,
        retry_count_init: int,
        auto_responses_init: int,
        lt_gov_init: bool,
        claim_violations_init: list[str] | None,
        missing_test_coverage_init: list[str] | None,
        evals_init: list[dict] | None,
    ) -> None:
        self.tests.passed = tests_passed_init
        self.tests.ran = list(tests_ran_init or [])
        self.tests.output = test_output_init
        self.tests.no_tests_found = no_tests_found_init
        self.tests.timed_out = timed_out_init
        self.freshness_info.status = freshness_init
        self.freshness_info.commits_since_base = commits_since_base_init
        self.freshness_info.overlapping_files = list(overlapping_files_init or [])
        self.freshness_info.pane_age_hours = pane_age_hours_init
        self.freshness_info.stale_files = list(stale_files_init or [])
        self.automation.retry_count = retry_count_init
        self.automation.auto_responses = auto_responses_init
        self.automation.lt_gov = lt_gov_init
        self.contract.claim_violations = list(claim_violations_init or [])
        self.contract.missing_test_coverage = list(missing_test_coverage_init or [])
        self.contract.evals = list(evals_init) if evals_init is not None else None

    @property
    def tests_passed(self) -> bool | None:
        return self.tests.passed

    @tests_passed.setter
    def tests_passed(self, value: bool | None) -> None:
        self.tests.passed = value

    @property
    def tests_ran(self) -> list[str]:
        return self.tests.ran

    @tests_ran.setter
    def tests_ran(self, value: list[str]) -> None:
        self.tests.ran = value

    @property
    def test_output(self) -> str:
        return self.tests.output

    @test_output.setter
    def test_output(self, value: str) -> None:
        self.tests.output = value

    @property
    def no_tests_found(self) -> bool:
        return self.tests.no_tests_found

    @no_tests_found.setter
    def no_tests_found(self, value: bool) -> None:
        self.tests.no_tests_found = value

    @property
    def timed_out(self) -> bool:
        return self.tests.timed_out

    @timed_out.setter
    def timed_out(self, value: bool) -> None:
        self.tests.timed_out = value

    @property
    def freshness(self) -> str | None:
        return self.freshness_info.status

    @freshness.setter
    def freshness(self, value: str | None) -> None:
        self.freshness_info.status = value

    @property
    def commits_since_base(self) -> int:
        return self.freshness_info.commits_since_base

    @commits_since_base.setter
    def commits_since_base(self, value: int) -> None:
        self.freshness_info.commits_since_base = value

    @property
    def overlapping_files(self) -> list[str]:
        return self.freshness_info.overlapping_files

    @overlapping_files.setter
    def overlapping_files(self, value: list[str]) -> None:
        self.freshness_info.overlapping_files = value

    @property
    def pane_age_hours(self) -> float:
        return self.freshness_info.pane_age_hours

    @pane_age_hours.setter
    def pane_age_hours(self, value: float) -> None:
        self.freshness_info.pane_age_hours = value

    @property
    def stale_files(self) -> list[str]:
        return self.freshness_info.stale_files

    @stale_files.setter
    def stale_files(self, value: list[str]) -> None:
        self.freshness_info.stale_files = value

    @property
    def retry_count(self) -> int:
        return self.automation.retry_count

    @retry_count.setter
    def retry_count(self, value: int) -> None:
        self.automation.retry_count = value

    @property
    def auto_responses(self) -> int:
        return self.automation.auto_responses

    @auto_responses.setter
    def auto_responses(self, value: int) -> None:
        self.automation.auto_responses = value

    @property
    def lt_gov(self) -> bool:
        return self.automation.lt_gov

    @lt_gov.setter
    def lt_gov(self, value: bool) -> None:
        self.automation.lt_gov = value

    @property
    def claim_violations(self) -> list[str]:
        return self.contract.claim_violations

    @claim_violations.setter
    def claim_violations(self, value: list[str]) -> None:
        self.contract.claim_violations = value

    @property
    def missing_test_coverage(self) -> list[str]:
        return self.contract.missing_test_coverage

    @missing_test_coverage.setter
    def missing_test_coverage(self, value: list[str]) -> None:
        self.contract.missing_test_coverage = value

    @property
    def evals(self) -> list[dict] | None:
        return self.contract.evals

    @evals.setter
    def evals(self, value: list[dict] | None) -> None:
        self.contract.evals = value

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "verdict": self.verdict,
            "commit_count": self.commit_count,
            "branch": self.branch,
            "stat": self.stat,
            "diff": self.diff,
            "commit_log": self.commit_log,
            "uncommitted": self.uncommitted,
            "files_changed": self.files_changed,
            "changed_files": self.changed_files,
            "protected_touched": self.protected_touched,
            "issues": self.issues,
            "error": self.error,
            "tests_passed": self.tests_passed,
            "tests_ran": self.tests_ran,
            "test_output": self.test_output,
            "no_tests_found": self.no_tests_found,
            "timed_out": self.timed_out,
            "freshness": self.freshness,
            "commits_since_base": self.commits_since_base,
            "overlapping_files": self.overlapping_files,
            "pane_age_hours": self.pane_age_hours,
            "stale_files": self.stale_files,
            "retry_count": self.retry_count,
            "auto_responses": self.auto_responses,
            "lt_gov": self.lt_gov,
            "claim_violations": self.claim_violations,
            "missing_test_coverage": self.missing_test_coverage,
            "evals": self.evals,
        }


def _inspect_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    full: bool = False,
    tests_pass: bool = True,
    lint_clean: bool = True,
    post_merge_check: str = "",
) -> ReviewInfo:
    """Inspect a worker pane's changes without emitting events.

    Returns typed ReviewInfo with diff stat, protected file status,
    commit log, and safe-to-merge verdict.
    With ``full=True``, includes the complete diff.
    This is a pure function that does not emit events.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)
    if not target:
        return ReviewInfo(slug=slug, error=f"Pane not found: {slug}")

    if target.get("role") == "lt-gov":
        return ReviewInfo(slug=slug, verdict="safe", lt_gov_init=True)

    wt = target.get("worktree_path", "")
    branch = target.get("branch_name", "")
    base_sha = target.get("base_sha", "")

    if not wt or not Path(wt).exists():
        return ReviewInfo(slug=slug, error=f"Worktree not found: {wt}")
    if not base_sha:
        return ReviewInfo(slug=slug, error="No base_sha recorded — cannot compute diff")

    # Run 4 independent git reads in parallel (saves ~3 fork latencies)
    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True)

    range_spec = f"{base_sha}..HEAD"
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_stat = pool.submit(_git, "diff", "--stat", range_spec)
        f_names = pool.submit(_git, "diff", "--name-only", range_spec)
        f_log = pool.submit(_git, "log", "--oneline", range_spec)
        f_porcelain = pool.submit(_git, "status", "--porcelain")
        f_full = pool.submit(_git, "diff", range_spec) if full else None

    stat_result = f_stat.result()
    stat = stat_result.stdout.strip() if stat_result.returncode == 0 else ""

    names_result = f_names.result()
    changed_files = (
        set(names_result.stdout.strip().splitlines()) if names_result.returncode == 0 else set()
    )
    protected_touched = sorted(changed_files & PROTECTED_FILES)

    log_result = f_log.result()
    commit_log = log_result.stdout.strip() if log_result.returncode == 0 else ""
    commit_count = len(commit_log.splitlines()) if commit_log else 0

    porcelain = f_porcelain.result()
    # Filter out protected files and worker instruction files.
    # CLAUDE.md and AGENTS.md are worktree-local and should not downgrade safe verdicts.
    porcelain_lines = []
    for ln in porcelain.stdout.splitlines():
        filename = ln[3:]
        # Skip protected files
        if any(filename.startswith(pf) for pf in PROTECTED_FILES):
            continue
        # Skip worker instruction files (worktree-local, not source changes)
        if filename in ("CLAUDE.md", "AGENTS.md"):
            continue
        # Skip dgov infrastructure files
        if filename.startswith(".dgov/"):
            continue
        porcelain_lines.append(ln)
    uncommitted = bool(porcelain_lines)

    # Verdict issues
    issues = []
    if protected_touched:
        issues.append(f"protected files touched: {protected_touched}")
    if uncommitted:
        issues.append("uncommitted changes (merge refused until committed)")
    if commit_count == 0:
        issues.append("no commits — nothing to merge")

    # Deterministic quality gates
    test_res = {}
    if tests_pass and commit_count > 0:
        test_res = _run_related_tests(wt, list(changed_files))
        if test_res.get("tests_passed") is False:
            issues.append(f"tests failed: {test_res.get('test_output', 'unknown error')}")

    if lint_clean and commit_count > 0:
        lint_res = _apply_lint_fixes(wt, list(changed_files))
        if not lint_res["passed"]:
            issues.append(f"lint failed: {lint_res['output']}")

    if post_merge_check and commit_count > 0:
        custom_res = _run_custom_check(wt, post_merge_check)
        if not custom_res["passed"]:
            issues.append(f"custom check failed: {custom_res['output']}")

    verdict = "safe" if not issues else "review"

    freshness = _compute_freshness(project_root, target, worker_changed_files=changed_files)

    # Load events once, derive both counters from one pass
    from dgov.recovery import _count_retries

    events = read_events(session_root, limit=500)
    retry_count = _count_retries(session_root, slug, events=events)
    auto_respond_count = sum(
        1 for ev in events if ev.get("event") == "pane_auto_responded" and ev.get("pane") == slug
    )

    result = ReviewInfo(
        slug=slug,
        branch=branch,
        stat=stat,
        protected_touched=list(protected_touched),
        verdict=verdict,
        commit_count=commit_count,
        commit_log=commit_log,
        uncommitted=uncommitted,
        files_changed=len(changed_files),
        changed_files=sorted(changed_files),
        issues=issues,
        retry_count_init=retry_count,
        auto_responses_init=auto_respond_count,
        freshness_init=freshness.get("freshness"),
        commits_since_base_init=freshness.get("commits_since_base", 0),
        overlapping_files_init=freshness.get("overlapping_files", []),
        pane_age_hours_init=freshness.get("pane_age_hours", 0.0),
    )
    if f_full is not None:
        diff_result = f_full.result()
        result.diff = diff_result.stdout if diff_result.returncode == 0 else ""

    if test_res:
        result.tests_ran = list(test_res.get("tests_ran", []))
        result.tests_passed = test_res.get("tests_passed")
        result.test_output = test_res.get("test_output", "")
        result.no_tests_found = test_res.get("no_tests_found", False)
        result.timed_out = test_res.get("timed_out", False)

    return result


def review_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    full: bool = False,
    tests_pass: bool = True,
    lint_clean: bool = True,
    post_merge_check: str = "",
) -> ReviewInfo:
    """Preview a worker pane's changes before merging.

    Returns diff stat, protected file status, commit log, and safe-to-merge verdict.
    With ``full=True``, includes the complete diff. Emits review_pass or review_fail events.
    """
    result = _inspect_worker_pane(
        project_root, slug, session_root, full, tests_pass, lint_clean, post_merge_check
    )
    if not result.error:
        sr = os.path.abspath(session_root or project_root)
        if result.verdict == "safe":
            emit_event(sr, "review_pass", slug, commit_count=result.commit_count)
        else:
            emit_event(sr, "review_fail", slug, issues=result.issues)
    return result


def _apply_lint_fixes(project_root: str, changed_files: list[str]) -> dict:
    """Auto-fix lint issues and amend worker commit."""
    python_files = [f for f in changed_files if f.endswith(".py")]
    if not python_files:
        return {"passed": True, "output": "no python files to lint"}

    # Auto-fix: ruff check --fix + ruff format
    subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", *python_files],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    subprocess.run(
        ["uv", "run", "ruff", "format", *python_files],
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    # If auto-fix changed files, amend the worker's last commit
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    if diff.stdout.strip():
        subprocess.run(["git", "add", *python_files], cwd=project_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=project_root,
            capture_output=True,
        )

    # Now verify: should be clean after auto-fix
    cmd = ["uv", "run", "ruff", "check", *python_files]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
    return {"passed": result.returncode == 0, "output": result.stdout + result.stderr}


def _run_custom_check(project_root: str, command: str) -> dict:
    """Run a custom shell command check."""
    result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=project_root)
    return {"passed": result.returncode == 0, "output": result.stdout + result.stderr}


def diff_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    stat: bool = False,
    name_only: bool = False,
) -> dict:
    """Get the diff for a worker pane's branch vs its base_sha."""
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    wt = target.get("worktree_path", "")
    base_sha = target.get("base_sha", "")
    if not wt or not Path(wt).exists():
        return {"error": f"Worktree not found: {wt}"}
    if not base_sha:
        return {"error": "No base_sha recorded"}

    cmd = ["git", "-C", wt, "diff", f"{base_sha}..HEAD"]
    if stat:
        cmd.append("--stat")
    elif name_only:
        cmd.append("--name-only")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": result.stderr.strip()}

    return {"slug": slug, "base_sha": base_sha, "diff": result.stdout}


def rebase_governor(project_root: str, onto: str | None = None) -> dict:
    """Rebase the current worktree onto a base branch.

    Args:
        project_root: Git repo (worktree) to rebase.
        onto: Explicit base branch. Auto-detects from upstream if None.

    Stashes dirty changes, rebases, and pops stash on success.
    On conflict: aborts rebase, pops stash, returns error.
    """
    project_root = os.path.abspath(project_root)

    # Detect base branch
    if onto:
        base = onto
    else:
        upstream = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            text=True,
        )
        if upstream.returncode == 0 and upstream.stdout.strip():
            base = upstream.stdout.strip().split("/", 1)[-1]  # origin/main -> main
        else:
            base = "main"

    # Stash if dirty
    status = subprocess.run(
        ["git", "-C", project_root, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    dirty = bool(status.stdout.strip())
    stashed = False
    if dirty:
        stash = subprocess.run(
            ["git", "-C", project_root, "stash", "push", "-m", "dgov-rebase-auto"],
            capture_output=True,
            text=True,
        )
        stashed = stash.returncode == 0

    # Fetch to ensure we have latest refs
    subprocess.run(
        ["git", "-C", project_root, "fetch", "--quiet"],
        capture_output=True,
        timeout=30,
    )

    # Rebase
    rebase = subprocess.run(
        ["git", "-C", project_root, "rebase", base],
        capture_output=True,
        text=True,
    )

    if rebase.returncode != 0:
        # Abort rebase
        subprocess.run(
            ["git", "-C", project_root, "rebase", "--abort"],
            capture_output=True,
        )
        # Pop stash if we stashed
        if stashed:
            subprocess.run(
                ["git", "-C", project_root, "stash", "pop"],
                capture_output=True,
            )
        return {
            "rebased": False,
            "base": base,
            "stashed": stashed,
            "error": rebase.stderr.strip() or "Rebase failed with conflicts",
        }

    # Pop stash on success
    if stashed:
        pop = subprocess.run(
            ["git", "-C", project_root, "stash", "pop"],
            capture_output=True,
            text=True,
        )
        if pop.returncode != 0:
            return {
                "rebased": True,
                "base": base,
                "stashed": True,
                "warning": "Rebase succeeded but stash pop had conflicts",
            }

    return {"rebased": True, "base": base, "stashed": stashed}


def check_test_coverage(changed_files: list[str], session_root: str = "") -> list[str]:
    """Check if changed source files have corresponding test files in the diff.

    Uses .test-manifest.json to look up expected test files.
    Returns list of source files missing test coverage.
    """
    # Load test manifest
    manifest_path = Path(session_root or ".") / ".test-manifest.json"
    if not manifest_path.is_file():
        manifest_path = Path(".test-manifest.json")
    if not manifest_path.is_file():
        return []  # No manifest, can't check

    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    # Find source files (not test files) that changed
    changed_set = set(changed_files)
    source_files = [
        f for f in changed_files if f.startswith("src/") and not f.startswith("tests/")
    ]
    test_files_changed = {f for f in changed_files if f.startswith("tests/")}

    missing = []
    for src in source_files:
        expected_tests = manifest.get(src, [])
        if expected_tests and not any(
            t in changed_set or t in test_files_changed for t in expected_tests
        ):
            missing.append(src)

    return missing


# ---------------------------------------------------------------------------
# Aggregate statistics
# ---------------------------------------------------------------------------
_FAILURE_STATES = frozenset({"failed", "abandoned", "escalated"})
_SUCCESS_STATES = frozenset({"merged"})


def compute_stats(session_root: str) -> dict:
    """Compute aggregate stats from pane records and events."""
    from dgov.persistence import all_panes, read_events

    panes = all_panes(session_root)
    events = read_events(session_root)

    # -- by_state --
    by_state: dict[str, int] = defaultdict(int)
    for p in panes:
        by_state[p["state"]] += 1

    # -- by_agent (normalize physical → logical names) --
    from dgov.router import physical_to_logical

    agent_panes: dict[str, list[dict]] = defaultdict(list)
    for p in panes:
        agent_panes[physical_to_logical(p["agent"])].append(p)

    # Build per-slug event index for duration calculation
    slug_events: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        slug_events[ev["pane"]].append(ev)

    by_agent: dict[str, dict] = {}
    for agent, agent_pane_list in agent_panes.items():
        successes = sum(1 for p in agent_pane_list if p["state"] in _SUCCESS_STATES)
        failures = sum(1 for p in agent_pane_list if p["state"] in _FAILURE_STATES)
        total = len(agent_pane_list)
        success_rate = successes / total if total else 0.0

        durations: list[float] = []
        for p in agent_pane_list:
            evs = slug_events.get(p["slug"], [])
            created_ts = _find_event_ts(evs, "pane_created")
            end_ts = _find_event_ts(evs, "pane_merged") or _find_event_ts(evs, "pane_done")
            if created_ts and end_ts:
                dur = (end_ts - created_ts).total_seconds()
                if dur >= 0:
                    durations.append(dur)

        avg_duration_s = sum(durations) / len(durations) if durations else None

        by_agent[agent] = {
            "total": total,
            "success_rate": round(success_rate, 4),
            "avg_duration_s": round(avg_duration_s, 2) if avg_duration_s is not None else None,
            "failures": failures,
        }

    # -- recent_failures --
    failure_panes = [p for p in panes if p["state"] in _FAILURE_STATES]
    # Sort by last event timestamp descending
    for p in failure_panes:
        evs = slug_events.get(p["slug"], [])
        p["_last_event_ts"] = evs[-1]["ts"] if evs else ""

    failure_panes.sort(key=lambda p: p["_last_event_ts"], reverse=True)

    recent_failures = [
        {
            "slug": p["slug"],
            "agent": p["agent"],
            "state": p["state"],
            "last_event_ts": p["_last_event_ts"],
        }
        for p in failure_panes[:5]
    ]

    try:
        from dgov.spans import agent_reliability_stats

        reliability = agent_reliability_stats(session_root, min_dispatches=1)
    except Exception:
        logger.debug("reliability stats failed", exc_info=True)
        reliability = {}

    return {
        "total_panes": len(panes),
        "by_state": dict(by_state),
        "by_agent": by_agent,
        "recent_failures": recent_failures,
        "event_count": len(events),
        "reliability": reliability,
    }


def _find_event_ts(events: list[dict], event_name: str) -> datetime | None:
    """Find the first event with the given name and parse its timestamp."""
    for ev in events:
        if ev["event"] == event_name:
            try:
                return datetime.fromisoformat(ev["ts"])
            except (ValueError, KeyError):
                return None
    return None


def _run_related_tests(project_root: str, changed_files: list[str]) -> dict:
    """Run pytest on test files related to changed source files.

    Maps src/dgov/X.py -> tests/test_X.py. Returns empty dict if no
    related tests found. On timeout, terminates the process group and
    returns structured failed test metadata instead of raising.
    """
    import signal

    test_files: list[str] = []
    for f in changed_files:
        if f.startswith("tests/") and f.endswith(".py"):
            abs_path = str(Path(project_root) / f)
            if Path(abs_path).exists():
                test_files.append(abs_path)
        elif f.startswith("src/dgov/") and f.endswith(".py"):
            name = Path(f).stem
            candidate = Path(project_root) / "tests" / f"test_{name}.py"
            if candidate.exists():
                test_files.append(str(candidate))
    if not test_files:
        logger.warning("No related tests found for changed files: %s", changed_files)
        return {"no_tests_found": True, "changed_files": list(changed_files)}
    test_files = sorted(set(test_files))

    # Use Popen with process group control so we can kill children on timeout
    cmd = ["uv", "run", "pytest", "-q", "-m", "unit", *test_files]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=project_root,
        start_new_session=True,  # Create new process group
    )
    try:
        stdout, stderr = proc.communicate(timeout=120)
        output = (stdout + stderr).strip()
        return {
            "tests_ran": [str(Path(f).relative_to(project_root)) for f in test_files],
            "tests_passed": proc.returncode == 0,
            "test_output": output[-500:] if len(output) > 500 else output,
        }
    except subprocess.TimeoutExpired:
        # Kill the entire process group to prevent orphans
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        # Wait for process to reap
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        return {
            "tests_ran": [str(Path(f).relative_to(project_root)) for f in test_files],
            "tests_passed": False,
            "test_output": "Tests timed out after 120s and were terminated",
            "timed_out": True,
        }
