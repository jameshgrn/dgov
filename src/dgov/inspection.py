"""Pane inspection: review, diff, rebase."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dgov.persistence import (
    PROTECTED_FILES,
    emit_event,
    get_pane,
    read_events,
)
from dgov.status import _compute_freshness


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
    # Filter out protected files — modified by worktree hook, not by worker
    porcelain_lines = []
    for ln in porcelain.stdout.strip().splitlines():
        filename = ln[3:]
        if not any(filename.startswith(pf) for pf in PROTECTED_FILES):
            porcelain_lines.append(ln)
    uncommitted = bool(porcelain_lines)

    # Verdict
    issues = []
    if protected_touched:
        issues.append(f"protected files touched: {protected_touched}")
    if uncommitted:
        issues.append("uncommitted changes (will be auto-committed on merge)")
    if commit_count == 0:
        issues.append("no commits — nothing to merge")

    verdict = "safe" if not issues else "review"

    if verdict == "safe":
        emit_event(session_root, "review_pass", slug)
    else:
        emit_event(session_root, "review_fail", slug, issues=issues)

    freshness = _compute_freshness(project_root, target, worker_changed_files=changed_files)

    # Load events once, derive both counters from one pass
    from dgov.retry import _count_retries

    events = read_events(session_root, limit=500)
    retry_count = _count_retries(session_root, slug, events=events)
    auto_respond_count = sum(
        1 for ev in events if ev.get("event") == "pane_auto_responded" and ev.get("pane") == slug
    )

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
