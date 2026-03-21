"""Pane inspection: review, diff, rebase."""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dgov.persistence import (
    PROTECTED_FILES,
    emit_event,
    get_pane,
    read_events,
)
from dgov.status import _compute_freshness


@dataclass
class MergeResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    conflicts: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def review_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    full: bool = False,
) -> dict:
    """Preview a worker pane's changes before merging.

    Returns diff stat, protected file status, commit log, and safe-to-merge verdict.
    With ``full=True``, includes the complete diff.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    wt = target.get("worktree_path", "")
    branch = target.get("branch_name", "")
    base_sha = target.get("base_sha", "")

    if not wt or not Path(wt).exists():
        return {"error": f"Worktree not found: {wt}"}
    if not base_sha:
        return {"error": "No base_sha recorded — cannot compute diff"}

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

    # Verdict
    issues = []
    if protected_touched:
        issues.append(f"protected files touched: {protected_touched}")
    if uncommitted:
        issues.append("uncommitted changes (merge refused until committed)")
    if commit_count == 0:
        issues.append("no commits — nothing to merge")

    verdict = "safe" if not issues else "review"

    if verdict == "safe":
        emit_event(session_root, "review_pass", slug)
    else:
        emit_event(session_root, "review_fail", slug, issues=issues)

    freshness = _compute_freshness(project_root, target, worker_changed_files=changed_files)

    # Load events once, derive both counters from one pass
    from dgov.recovery import _count_retries

    events = read_events(session_root, limit=500)
    retry_count = _count_retries(session_root, slug, events=events)
    auto_respond_count = sum(
        1 for ev in events if ev.get("event") == "pane_auto_responded" and ev.get("pane") == slug
    )

    # Run smoke tests on related test files
    changed_file_list = list(changed_files)
    test_result = _run_related_tests(wt, changed_file_list)

    result = {
        "slug": slug,
        "branch": branch,
        "stat": stat,
        "protected_touched": protected_touched,
        "verdict": verdict,
        "commit_count": commit_count,
        "commit_log": commit_log,
        "uncommitted": uncommitted,
        "files_changed": len(changed_files),
        "retry_count": retry_count,
        "auto_responses": auto_respond_count,
        **freshness,
    }
    if issues:
        result["issues"] = issues
    if f_full is not None:
        diff_result = f_full.result()
        result["diff"] = diff_result.stdout if diff_result.returncode == 0 else ""
    if test_result:
        result.update(test_result)

    return result


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

    # -- by_agent --
    agent_panes: dict[str, list[dict]] = defaultdict(list)
    for p in panes:
        agent_panes[p["agent"]].append(p)

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

    reliability = compute_reliability_stats(session_root)

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


def compute_reliability_stats(session_root: str) -> dict:
    """Compute reliability stats from decision journal data.

    Returns {'by_agent': {...}, 'by_provider': {...}} where:
    - by_agent: per-agent review_pass_rate, avg_retry_count, test_pass_rate
    - by_provider: per-provider call_count, error_rate, avg_latency_ms
    """
    # Load decision journal and pane data
    from dgov.persistence import all_panes, read_decision_journal

    records = read_decision_journal(session_root)
    panes = all_panes(session_root)

    # Build slug -> agent map
    slug_to_agent: dict[str, str] = {}
    for p in panes:
        slug_to_agent[p["slug"]] = p["agent"]

    # -- by_agent stats (from review_output records) --
    agent_data: dict[str, dict] = defaultdict(
        lambda: {"safe_count": 0, "review_count": 0, "retry_counts": [], "test_passes": []}
    )

    for rec in records:
        if rec.get("kind") != "review_output":
            continue
        result = rec.get("result")
        if not result:
            continue
        decision = result.get("decision", {})
        artifact = result.get("artifact", {}) or {}

        pane_slug = rec.get("pane_slug")
        agent = slug_to_agent.get(pane_slug, "unknown")

        verdict = decision.get("verdict", "")
        retry_count = artifact.get("retry_count", 0)

        if verdict == "safe":
            agent_data[agent]["safe_count"] += 1
        else:
            agent_data[agent]["review_count"] += 1

        agent_data[agent]["retry_counts"].append(retry_count)

        # tests_passed may be missing — skip if absent
        tests_passed = artifact.get("tests_passed")
        if tests_passed is not None:
            agent_data[agent]["test_passes"].append(tests_passed)

    by_agent: dict[str, dict] = {}
    for agent, data in agent_data.items():
        total_reviews = data["safe_count"] + data["review_count"]
        review_pass_rate = data["safe_count"] / total_reviews if total_reviews > 0 else 0.0

        avg_retry_count = (
            sum(data["retry_counts"]) / len(data["retry_counts"]) if data["retry_counts"] else None
        )

        test_pass_values = [1.0 if tp else 0.0 for tp in data["test_passes"]]
        test_pass_rate = (
            sum(test_pass_values) / len(test_pass_values) if test_pass_values else None
        )

        by_agent[agent] = {
            "review_pass_rate": round(review_pass_rate, 4) if review_pass_rate != 0.0 else 0.0,
            "avg_retry_count": round(avg_retry_count, 2) if avg_retry_count is not None else None,
            "test_pass_rate": round(test_pass_rate, 4) if test_pass_rate is not None else None,
        }

    # -- by_provider stats (from ALL records) --
    provider_data: dict[str, dict] = defaultdict(
        lambda: {"call_count": 0, "error_count": 0, "latencies": []}
    )

    for rec in records:
        provider_id = rec.get("provider_id")
        if not provider_id:
            continue

        provider_data[provider_id]["call_count"] += 1

        if rec.get("error") is not None:
            provider_data[provider_id]["error_count"] += 1

        latency = rec.get("duration_ms")
        if latency is not None:
            provider_data[provider_id]["latencies"].append(latency)

    by_provider: dict[str, dict] = {}
    for provider_id, data in provider_data.items():
        call_count = data["call_count"]
        error_rate = data["error_count"] / call_count if call_count > 0 else 0.0
        avg_latency_ms = (
            sum(data["latencies"]) / len(data["latencies"]) if data["latencies"] else None
        )

        by_provider[provider_id] = {
            "call_count": call_count,
            "error_rate": round(error_rate, 4) if error_rate != 0.0 else 0.0,
            "avg_latency_ms": round(avg_latency_ms, 2) if avg_latency_ms is not None else None,
        }

    return {"by_agent": dict(by_agent), "by_provider": dict(by_provider)}


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
        return {}
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
