"""Pane status: list, freshness, output capture, pruning."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path

from dgov.backend import get_backend
from dgov.gitops import _remove_worktree
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    get_pane,
    remove_pane,
)
from dgov.waiter import _is_done

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _read_last_output_from_log(session_root: str, slug: str) -> str:
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    if not log_path.exists():
        return ""

    try:
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            lines: deque[str] = deque(maxlen=3)
            for line in handle:
                lines.append(_strip_ansi(line.rstrip("\r\n")))
    except OSError:
        return ""

    return "\n".join(lines)


# -- Freshness --


def _compute_freshness(project_root: str, pane_record: dict) -> dict:
    """Compute freshness score for a pane relative to main.

    Returns {"freshness": "fresh"|"warn"|"stale", "commits_since_base": int,
             "overlapping_files": [...], "pane_age_hours": float}
    """
    base_sha = pane_record.get("base_sha", "")
    created_at = pane_record.get("created_at", 0)
    wt = pane_record.get("worktree_path", "")

    age_hours = (time.time() - created_at) / 3600 if created_at else 0

    # Early exit: if worktree is gone, it's stale — skip all git calls
    if wt and not Path(wt).exists():
        return {
            "freshness": "stale",
            "commits_since_base": 0,
            "overlapping_files": [],
            "pane_age_hours": round(age_hours, 1),
        }

    # Commits on main since base
    commits_since = 0
    if base_sha:
        log_result = subprocess.run(
            ["git", "-C", project_root, "log", f"{base_sha}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
        )
        if log_result.returncode == 0:
            commits_since = len([ln for ln in log_result.stdout.strip().splitlines() if ln])

    # Files changed on main since base
    main_files: set[str] = set()
    if base_sha:
        main_diff = subprocess.run(
            ["git", "-C", project_root, "diff", "--name-only", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        if main_diff.returncode == 0:
            main_files = set(main_diff.stdout.strip().splitlines())

    # Files changed on worker branch
    worker_files: set[str] = set()
    if wt and Path(wt).exists() and base_sha:
        worker_diff = subprocess.run(
            ["git", "-C", wt, "diff", "--name-only", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        if worker_diff.returncode == 0:
            worker_files = set(worker_diff.stdout.strip().splitlines())

    overlap = sorted(main_files & worker_files)

    # Classification
    if overlap and (commits_since > 5 or age_hours > 12):
        freshness = "stale"
    elif overlap or commits_since > 0 or age_hours > 4:
        freshness = "warn"
    else:
        freshness = "fresh"

    return {
        "freshness": freshness,
        "commits_since_base": commits_since,
        "overlapping_files": overlap,
        "pane_age_hours": round(age_hours, 1),
    }


# -- Concurrency guard --


def _count_active_agent_workers(session_root: str, agent: str) -> int:
    """Count how many workers for *agent* are currently alive."""
    _TERMINAL_STATES = {
        "done",
        "failed",
        "superseded",
        "merged",
        "closed",
        "escalated",
        "timed_out",
    }
    panes = all_panes(session_root)
    all_tmux = get_backend().bulk_info()
    count = 0
    for p in panes:
        if p.get("agent") == agent and p.get("state") not in _TERMINAL_STATES:
            pane_id = p.get("pane_id", "")
            if pane_id and pane_id in all_tmux:
                count += 1
    return count


# -- Public API --


def list_worker_panes(
    project_root: str,
    session_root: str | None = None,
    *,
    include_freshness: bool = True,
) -> list[dict]:
    """List worker panes with live status from tmux.

    When *include_freshness* is False, skip the per-pane git subprocess calls
    that compute freshness (up to 3 git calls per pane). Use False in hot
    paths like dashboard refresh and preflight checks that don't need it.
    """
    session_root = os.path.abspath(session_root or project_root)
    panes = all_panes(session_root)
    all_tmux = get_backend().bulk_info()
    result = []
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        state = p.get("state") or "active"
        alive = pane_id in all_tmux if pane_id else False
        cmd = all_tmux.get(pane_id, {}).get("current_command", "") if alive else ""
        done = state != "active"
        if state == "active":
            done = _is_done(session_root, slug, pane_record=p)
            if done:
                # _is_done updated persistent state; reconcile local copy
                updated = get_pane(session_root, slug)
                if updated:
                    state = updated.get("state", state)
        if include_freshness:
            freshness = _compute_freshness(project_root, p)
        else:
            freshness = {
                "freshness": "unknown",
                "commits_since_base": 0,
                "overlapping_files": [],
                "pane_age_hours": 0,
            }
        # Determine worker activity
        activity = "unknown"
        if not alive:
            activity = "exited"
        elif done:
            activity = "done"
        else:
            cmd_lower = cmd.strip().lower()
            agent_cmds = {
                "claude",
                "codex",
                "gemini",
                "opencode",
                "cline",
                "qwen",
                "amp",
                "pi",
                "cursor-agent",
                "copilot",
                "crush",
                "node",
                "python",
                "python3",
            }
            if cmd_lower in agent_cmds:
                activity = "working"
            elif cmd_lower in ("zsh", "bash", "sh", "fish"):
                activity = "idle"
            elif cmd_lower:
                activity = cmd_lower[:15]

        last_output = _read_last_output_from_log(session_root, slug)

        entry: dict = {
            "slug": slug,
            "agent": p.get("agent"),
            "pane_id": pane_id,
            "alive": alive,
            "done": done,
            "state": state,
            "activity": activity,
            "last_output": last_output,
            "current_command": cmd,
            "worktree_path": p.get("worktree_path"),
            "branch": p.get("branch_name"),
            "prompt": p.get("prompt", "")[:80],
            "duration_s": round(time.time() - (p.get("created_at") or time.time())),
            **freshness,
        }
        result.append(entry)

    # Deduplicate by slug: prefer alive entry, then latest (last in list)
    seen: dict[str, int] = {}
    for i, entry in enumerate(result):
        slug = entry["slug"]
        if slug not in seen:
            seen[slug] = i
        else:
            prev = result[seen[slug]]
            # Prefer alive over dead; if both same liveness, keep latest
            if entry["alive"] and not prev["alive"]:
                seen[slug] = i
            elif entry["alive"] == prev["alive"]:
                seen[slug] = i  # latest wins
    return [result[i] for i in sorted(seen.values())]


def prune_stale_panes(project_root: str, session_root: str | None = None) -> list[str]:
    """Remove state entries for panes that are dead and have no worktree.

    Also removes orphaned worktree directories in ``.dgov/worktrees/`` that
    have no matching pane entry in state (e.g. left behind after ``pane close``
    skipped a dirty worktree).
    """
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    panes = all_panes(session_root)
    pruned = []

    # Pass 1: prune stale state entries (existing behaviour)
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        alive = get_backend().is_alive(pane_id) if pane_id else False
        wt = p.get("worktree_path", "")
        wt_exists = bool(wt) and Path(wt).exists()
        if not alive and not wt_exists:
            remove_pane(session_root, slug)
            done_path = Path(session_root) / STATE_DIR / "done" / slug
            done_path.unlink(missing_ok=True)
            pruned.append(slug)

    # Pass 2: remove orphaned worktree dirs with no matching pane entry
    worktrees_dir = Path(project_root) / STATE_DIR / "worktrees"
    if worktrees_dir.is_dir():
        # Re-read state after pass 1 removals
        remaining_panes = all_panes(session_root)
        known_worktrees = {p.get("worktree_path") for p in remaining_panes}
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            entry_str = str(entry)
            if entry_str in known_worktrees:
                continue
            # Orphan — no pane entry references this dir.
            # Only remove if there's no live tmux pane using it.
            branch_name = entry.name
            _remove_worktree(project_root, entry_str, branch_name)
            pruned.append(f"orphan:{branch_name}")

    return pruned


def capture_worker_output(
    project_root: str, slug: str, lines: int = 30, session_root: str | None = None
) -> str | None:
    """Capture the last N lines of a worker pane's output."""
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)

    if not target or not target.get("pane_id"):
        return None

    pane_id = target["pane_id"]
    if not get_backend().is_alive(pane_id):
        return None

    return get_backend().capture_output(pane_id, lines)


# -- ANSI stripping (lightweight, no curses dependency) --

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def tail_worker_log(session_root: str, slug: str, lines: int = 20) -> str | None:
    """Read the last *lines* lines from ``.dgov/logs/<slug>.log``.

    Seeks from the end of the file so large logs aren't fully loaded.
    Returns ``None`` if the log file doesn't exist.
    ANSI escape codes are stripped and the text is decoded with
    ``errors='replace'``.
    """
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    if not log_path.exists():
        return None

    try:
        size = log_path.stat().st_size
        if size == 0:
            return ""

        # Read a chunk from the end; 512 bytes per line is a generous estimate.
        chunk_size = min(size, lines * 512)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - chunk_size))
            raw = f.read()

        text = raw.decode("utf-8", errors="replace")

        # If we didn't read from the start, drop the first (likely partial) line
        if chunk_size < size:
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1 :]

        tail_lines = text.splitlines()[-lines:]
        return _strip_ansi("\n".join(tail_lines))
    except OSError:
        return None
