"""Pane status: list, freshness, output capture, pruning."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from dgov.backend import get_backend
from dgov.done import _AGENT_COMMANDS, _is_done, _strip_ansi
from dgov.gitops import _remove_worktree
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    emit_event,
    get_pane,
    get_preserved_artifacts,
    remove_pane,
)

logger = logging.getLogger(__name__)


_INTERNAL_BOOTSTRAP_RE = re.compile(
    r"(?:^|\s)source\s+/\S*dgov-cmd-[^\s/]+\.sh\b.*\brm\b.*\bdgov-cmd-[^\s/]+\.sh\b"
)


def _clean_worker_output_text(text: str) -> str:
    """Normalize worker output for user-facing tails and previews."""
    stripped = _strip_ansi(text)
    cleaned_lines: list[str] = []
    for raw_line in stripped.splitlines():
        normalized = " ".join(raw_line.split())
        if not normalized:
            continue
        if _INTERNAL_BOOTSTRAP_RE.search(normalized):
            continue
        if _is_noise_line(normalized):
            continue
        cleaned_lines.append(raw_line.rstrip())
    return "\n".join(cleaned_lines)


def _tail_text_file(path: Path, *, lines: int, bytes_per_line: int) -> str | None:
    """Read the tail of a text file without loading it entirely."""
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        if size == 0:
            return ""
        chunk_size = min(size, max(4096, lines * bytes_per_line))
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk_size))
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        if chunk_size < size:
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1 :]
        return text
    except OSError:
        return None


def _pi_session_dir(worktree_path: str) -> Path | None:
    """Return the Pi session directory for a worker worktree."""
    from dgov.spans import pi_session_dir_for_worktree

    return pi_session_dir_for_worktree(worktree_path)


def _latest_pi_transcript_path(worktree_path: str) -> Path | None:
    """Return the newest live Pi transcript for a worktree, if present."""
    from dgov.spans import latest_pi_transcript_path

    return latest_pi_transcript_path(worktree_path)


def _format_transcript_tool_call(tool_name: str, args: dict) -> str | None:
    """Render a concise human-readable line for a transcript tool call."""
    if tool_name == "read" and args.get("path"):
        return f"Reading {args['path']}"
    if tool_name == "edit" and args.get("path"):
        return f"Editing {args['path']}"
    if tool_name in {"write", "create"} and args.get("path"):
        return f"Writing {args['path']}"
    if tool_name == "bash" and args.get("command"):
        return f"Running {str(args['command'])[:120]}"
    if tool_name:
        return tool_name
    return None


def _tail_live_transcript(session_root: str, slug: str, lines: int = 20) -> str | None:
    """Render a readable tail from the live Pi transcript for *slug*."""
    pane = get_pane(session_root, slug)
    if not pane:
        return None
    transcript_path = _latest_pi_transcript_path(str(pane.get("worktree_path", "")))
    if transcript_path is None:
        return None

    raw_tail = _tail_text_file(transcript_path, lines=max(lines * 4, 40), bytes_per_line=2048)
    if raw_tail is None:
        return None

    rendered: list[str] = []
    for raw_line in raw_tail.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue

        message = entry.get("message", {})
        role = message.get("role", "")
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        if role == "assistant":
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    for text_line in str(item.get("text", "")).splitlines():
                        cleaned = _clean_worker_output_text(text_line).strip()
                        if cleaned:
                            rendered.append(cleaned[:200])
                elif item_type in {"toolCall", "tool_use"}:
                    args = item.get("arguments", item.get("input", {}))
                    if isinstance(args, dict):
                        summary = _format_transcript_tool_call(item.get("name", ""), args)
                        if summary:
                            rendered.append(summary)
        elif role == "toolResult":
            tool_name = str(message.get("toolName", ""))
            if tool_name == "read":
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = str(item.get("text", "")).strip()
                if not text or text == "(no output)":
                    continue
                text_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if len(text_lines) <= 3:
                    rendered.extend(text_lines[:3])

    if not rendered:
        return None

    compact: list[str] = []
    for line in rendered:
        if compact and compact[-1] == line:
            continue
        compact.append(line)
    return "\n".join(compact[-lines:])


def _informative_line_count(text: str | None) -> int:
    """Count non-noise lines in a worker output sample."""
    if not text:
        return 0
    count = 0
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized or _is_noise_line(normalized):
            continue
        if len(normalized) <= 2 and " " not in normalized:
            continue
        count += 1
    return count


def tail_worker_log(session_root: str, slug: str, lines: int = 20) -> str | None:
    """Read the last *lines* lines from ``.dgov/logs/<slug>.log``.

    Seeks from the end of the file so large logs aren't fully loaded.
    Returns ``None`` if the log file doesn't exist.
    ANSI escape codes are stripped and the text is decoded with
    ``errors='replace'``.
    """
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    raw_log = _tail_text_file(log_path, lines=lines, bytes_per_line=512)
    cleaned_log = None
    if raw_log is not None:
        cleaned_log = _clean_worker_output_text("\n".join(raw_log.splitlines()[-lines:]))

    transcript_tail = _tail_live_transcript(session_root, slug, lines=lines)
    if _informative_line_count(transcript_tail) > _informative_line_count(cleaned_log):
        return transcript_tail
    return cleaned_log


# -- Freshness --


def _compute_freshness(
    project_root: str,
    pane_record: dict,
    *,
    worker_changed_files: set[str] | None = None,
) -> dict:
    """Compute freshness score for a pane relative to main.

    If *worker_changed_files* is provided (from a prior review that already
    computed ``git diff --name-only``), the worker-side git call is skipped.

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
        main_files = (
            set(main_diff.stdout.strip().splitlines()) if main_diff.returncode == 0 else set()
        )
    else:
        main_files = set()

    # Files changed on worker branch (skip if caller already computed)
    if worker_changed_files is not None:
        worker_files = worker_changed_files
    elif wt and Path(wt).exists() and base_sha:
        worker_diff = subprocess.run(
            ["git", "-C", wt, "diff", "--name-only", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        worker_files = (
            set(worker_diff.stdout.strip().splitlines()) if worker_diff.returncode == 0 else set()
        )
    else:
        worker_files = set()

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
    include_prompt: bool = True,
) -> list[dict]:
    """List worker panes with live status from tmux.

    When *include_freshness* is False, skip the per-pane git subprocess calls
    that compute freshness (up to 3 git calls per pane). Use False in hot
    paths like dashboard refresh and preflight checks that don't need it.

    When *include_prompt* is False, use a slim DB query that only loads the
    first 200 characters of each prompt. Use False in hot paths like the
    dashboard where full prompts are not needed.

    Always reads worker logs and derives summary from them.
    """
    from dgov.agents import load_registry
    from dgov.persistence import list_panes_slim

    session_root = os.path.abspath(session_root or project_root)
    # Auto-prune stale panes before listing
    prune_stale_panes(project_root, session_root)
    panes = list_panes_slim(session_root) if not include_prompt else all_panes(session_root)
    all_tmux = get_backend().bulk_info()
    registry = load_registry(project_root)
    result = []
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        state = p.get("state") or "active"
        alive = pane_id in all_tmux if pane_id else False
        cmd = all_tmux.get(pane_id, {}).get("current_command", "") if alive else ""
        done = state != "active"
        if state == "active":
            agent_id = p.get("agent", "")
            agent_def = registry.get(agent_id) if agent_id else None
            agent_done_strategy = agent_def.done_strategy if agent_def else None
            done = _is_done(
                session_root,
                slug,
                pane_record=p,
                done_strategy=agent_done_strategy,
                alive=alive,
                current_command=cmd,
            )
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
            if cmd_lower in _AGENT_COMMANDS:
                activity = "working"
            elif cmd_lower in ("zsh", "bash", "sh", "fish"):
                activity = "idle"
            elif cmd_lower:
                activity = cmd_lower[:15]

        duration_s = round(time.time() - (p.get("created_at") or time.time()))

        # Always read worker output and derive summary from log text
        last_output = tail_worker_log(session_root, slug, lines=10) or ""
        summary = _extract_summary_from_log(session_root, slug, pre_read=last_output)

        phase = _compute_phase(state, alive, done, duration_s, summary)
        progress = _read_progress_json(session_root, slug)
        preserved_artifacts = get_preserved_artifacts(p)

        entry: dict = {
            "slug": slug,
            "agent": p.get("agent"),
            "pane_id": pane_id,
            "alive": alive,
            "done": done,
            "state": state,
            "activity": activity,
            "last_output": last_output,
            "summary": summary,
            "phase": phase,
            "progress": progress,
            "current_command": cmd,
            "worktree_path": p.get("worktree_path"),
            "branch": p.get("branch_name"),
            "base_sha": p.get("base_sha", ""),
            "prompt": p.get("prompt", "")[:80],
            "role": p.get("role", "worker"),
            "parent_slug": p.get("parent_slug", ""),
            "tier_id": p.get("tier_id", ""),
            "created_at": p.get("created_at"),
            "duration_s": duration_s,
            "preserved_artifacts": preserved_artifacts,
            "preserved_reason": preserved_artifacts.get("reason") if preserved_artifacts else None,
            "preserved_recoverable": (
                bool(preserved_artifacts.get("recoverable")) if preserved_artifacts else False
            ),
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


_TERMINAL_PRUNE_STATES = frozenset(
    {
        "abandoned",
        "closed",
        "done",
        "failed",
        "merged",
        "superseded",
        "timed_out",
    }
)
_TERMINAL_PRUNE_AGE_S = 3600  # 1 hour


def prune_stale_panes(project_root: str, session_root: str | None = None) -> list[str]:
    """Remove mechanically stale pane entries and orphaned worktrees.

    Also removes orphaned worktree directories in ``.dgov/worktrees/`` that
    have no matching pane entry in state (e.g. left behind after ``pane close``
    skipped a dirty worktree).
    """
    from dgov.lifecycle import _pane_lock

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    with _pane_lock(project_root):
        panes = all_panes(session_root)
        pruned: list[str] = []
        pruned_slugs: set[str] = set()

        # Get bulk tmux info once for efficiency
        bulk_info = get_backend().bulk_info()

        # Pass 1: prune stale state entries
        for p in panes:
            pane_id = p.get("pane_id", "")
            slug = p["slug"]
            state = p.get("state", "")
            # Skip panes claimed by --land lifecycle
            if p.get("landing"):
                continue
            alive = pane_id in bulk_info if pane_id else False
            wt = p.get("worktree_path", "")
            wt_exists = bool(wt) and Path(wt).exists()

            # If pane_id exists but is dead, and worktree is gone, it's stale.
            # If NO pane_id exists yet (monitor dispatching), but worktree exists, it's NOT stale.
            should_prune = False
            if pane_id:
                if not alive and not wt_exists:
                    should_prune = True
            else:
                if not wt_exists:
                    should_prune = True

            # Superseded/escalated panes were replaced — always force cleanup.
            # _close_replaced_pane should have removed them, but if it failed
            # silently the pane lingers with a live tmux process + worktree.
            if not should_prune and state in {"superseded", "escalated"}:
                should_prune = True
                if alive and pane_id:
                    try:
                        get_backend().destroy(pane_id)
                    except Exception:
                        logger.warning("Failed to destroy replaced pane %s (%s)", slug, pane_id)
                if wt_exists and wt:
                    branch = p.get("branch_name", slug)
                    _remove_worktree(project_root, wt, branch)

            if should_prune:
                remove_pane(session_root, slug)
                emit_event(session_root, "pane_pruned", slug, reason="stale")
                done_path = Path(session_root) / STATE_DIR / "done" / slug
                done_path.unlink(missing_ok=True)
                pruned.append(slug)
                pruned_slugs.add(slug)
            elif pane_id and not alive and wt_exists and state == "active":
                # Dead process with orphaned worktree — force to failed
                from dgov.persistence import settle_completion_state

                settle_completion_state(session_root, slug, "failed")
                emit_event(session_root, "pane_failed", slug, reason="dead_process_pruned")
                logger.info("Pruned dead active pane %s (worktree preserved)", slug)
                pruned.append(f"dead:{slug}")

        # Pass 2: remove orphaned worktree dirs (with grace period)
        # Scan both the new location (~/.dgov/worktrees/<hash>/) and the
        # legacy location (.dgov/worktrees/) for orphans.
        from dgov.lifecycle import _worktree_base

        _wt_dirs = [_worktree_base(project_root), Path(project_root) / STATE_DIR / "worktrees"]
        known_worktrees = {p.get("worktree_path") for p in panes if p["slug"] not in pruned_slugs}
        now = time.time()
        for worktrees_dir in _wt_dirs:
            if not worktrees_dir.is_dir():
                continue
            for entry in worktrees_dir.iterdir():
                if not entry.is_dir():
                    continue
                entry_str = str(entry)
                if entry_str in known_worktrees:
                    continue
                try:
                    age_s = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age_s < 60:
                    continue
                branch_name = entry.name
                _remove_worktree(project_root, entry_str, branch_name)
                pruned.append(f"orphan:{branch_name}")

        return pruned


def gc_retained_panes(
    project_root: str,
    session_root: str | None = None,
    *,
    older_than_s: float = _TERMINAL_PRUNE_AGE_S,
    states: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    """Explicitly garbage-collect preserved evidence and old terminal panes."""
    from dgov.executor import run_close_only
    from dgov.lifecycle import _pane_lock

    if older_than_s < 0:
        raise ValueError("older_than_s must be non-negative")

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    state_filter = set(states)
    now = time.time()
    pruned: list[str] = []
    skipped: list[str] = []
    candidates: list[str] = []

    with _pane_lock(project_root):
        for pane in all_panes(session_root):
            slug = str(pane.get("slug", ""))
            state = str(pane.get("state", ""))
            if not slug or state == "active":
                continue
            if state_filter and state not in state_filter:
                continue

            artifacts = get_preserved_artifacts(pane)
            if artifacts is None and state not in _TERMINAL_PRUNE_STATES:
                continue

            age_anchor = (
                float(artifacts.get("preserved_at", 0))
                if artifacts
                else float(pane.get("created_at", 0))
            )
            if now - age_anchor < older_than_s:
                continue
            candidates.append(slug)

        for slug in candidates:
            result = run_close_only(project_root, slug, session_root=session_root, force=True)
            if result.closed and get_pane(session_root, slug) is None:
                pruned.append(slug)
            else:
                skipped.append(slug)

    return {"pruned": pruned, "skipped": skipped}


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

    output = get_backend().capture_output(pane_id, lines)
    cleaned_output = _clean_worker_output_text(output) if output is not None else None
    transcript_tail = _tail_live_transcript(session_root, slug, lines=lines)
    if _informative_line_count(transcript_tail) > _informative_line_count(cleaned_output):
        return transcript_tail
    return cleaned_output


# -- Noise filtering --

_NOISE_RE: list[re.Pattern[str]] = [
    re.compile(r"^\s*$"),  # blank / whitespace-only
    re.compile(r"^[\u2500-\u257f\u2580-\u259f\u2800-\u28ff\s]+$"),  # box-drawing / braille
    re.compile(
        r"(?i)(?:"
        r"type your message|bypass permissions|shift\+tab|ctrl\+|YOLO|/model"
        r"|for shortcuts|MCP servers|Update available"
        r"|\[Opus|\[Sonnet|Sprouting|Cooking|Cooked for"
        r")"
    ),  # agent UI chrome
    re.compile(r"^[\$#>%\s]+$"),  # bare shell prompts
    re.compile(r"^\s*[➜❯❮⌵◉▶]\s+\w+"),  # arrow-style shell prompts (➜ slug git:(branch))
    re.compile(r"^\s*\d+pct\s*\|"),  # progress bars (N pct |)
    re.compile(r"^\s*\d+%\s*[\|█▓▒░]"),  # progress bars (N% |)
]


def _is_noise_line(line: str) -> bool:
    """Return True if *line* is TUI chrome or noise that should be filtered."""
    return any(pat.search(line) for pat in _NOISE_RE)


# -- Signal extraction --

_SIGNAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:Read|Reading)\s+(.+)"), "Reading {0}"),
    (re.compile(r"(?:Edit|Editing)\s+(.+)"), "Editing {0}"),
    (re.compile(r"(?:Write|Writing|Creating)\s+(.+)"), "Writing {0}"),
    (re.compile(r"(?:Running|Ran)\s+(ruff\b.*)"), "Linting: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(pytest\b.*)"), "Testing: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(uv\b.*)"), "Running: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(git\b.*)"), "Git: {0}"),
    (re.compile(r"git add\s+(.+)"), "Staging: {0}"),
    (re.compile(r"git commit\s+(.*)"), "Committing"),
    (re.compile(r"(\d+)\s+passed"), "{0} tests passed"),
    (re.compile(r"All checks passed|no issues found", re.IGNORECASE), "Lint clean"),
    (re.compile(r"(\d+)\s+files?\s+changed"), "{0} files changed"),
]


def _match_signal(line: str) -> str | None:
    """Try to match *line* against known signal patterns, return formatted string or None."""
    for pat, fmt in _SIGNAL_PATTERNS:
        m = pat.search(line)
        if m:
            groups = m.groups()
            if groups:
                formatted = fmt.format(*(g[:60] if g else "" for g in groups))
            else:
                formatted = fmt
            return formatted[:80]
    return None


def _extract_summary_from_log(
    session_root: str, slug: str, lines: int = 10, *, pre_read: str | None = None
) -> str:
    """Extract a clean one-line summary from the worker log tail."""
    raw = (
        pre_read
        if pre_read is not None
        else tail_worker_log(session_root, slug, lines=lines) or ""
    )
    if not raw:
        return ""
    stripped = _strip_ansi(raw)
    all_lines = stripped.splitlines()
    # Walk bottom-up, skip noise, try signal matching
    for line in reversed(all_lines):
        line = line.strip()
        if not line or _is_noise_line(line):
            continue
        sig = _match_signal(line)
        if sig:
            return sig
        # No signal match — return truncated non-noise line
        return line[:60]
    return ""


# -- Phase computation --


def _compute_phase(
    state: str,
    alive: bool,
    done: bool,
    duration_s: int,
    summary: str,
) -> str:
    """Derive a human-readable phase from worker state and summary."""
    _TERMINAL_MAP = {
        "failed": "failed",
        "merged": "merged",
        "closed": "closed",
        "superseded": "closed",
        "escalated": "failed",
        "timed_out": "failed",
    }
    if state in _TERMINAL_MAP:
        return _TERMINAL_MAP[state]
    if done:
        return "done"
    if not alive and state == "active":
        return "abandoned"
    if alive and duration_s < 30 and not summary:
        return "starting"
    summary_lower = summary.lower()
    if "test" in summary_lower or "pytest" in summary_lower:
        return "testing"
    if "staging" in summary_lower or "committing" in summary_lower:
        return "committing"
    if summary:
        return "working"
    return "idle"


# -- Progress JSON reader --


def _read_progress_json(session_root: str, slug: str) -> dict | None:
    """Read .dgov/progress/<slug>.json if present and recent (<60s)."""
    import json

    progress_path = Path(session_root) / STATE_DIR / "progress" / f"{slug}.json"
    if not progress_path.exists():
        return None
    try:
        mtime = progress_path.stat().st_mtime
        if time.time() - mtime > 60:
            return None
        data = json.loads(progress_path.read_text())
        if not isinstance(data, dict):
            return None
        # Normalize v1 vs legacy schema
        if "v" in data:
            # v1 schema: {v, phase, message}
            return {"phase": data.get("phase", ""), "message": data.get("message", "")}
        # Legacy schema: {status, message, turn}
        return {
            "phase": data.get("status", ""),
            "message": data.get("message", ""),
            "turn": data.get("turn"),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return None
