"""Done-signal and done-detection helpers.

Extracted from waiter.py to break the import cycle between
lifecycle/status and the heavy waiter module.  This module
only depends on backend and persistence (lower-level).
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.backend import get_backend
from dgov.persistence import STATE_DIR, PaneState

if TYPE_CHECKING:
    from dgov.agents import DoneStrategy

logger = logging.getLogger(__name__)


# -- ANSI stripping (lightweight, no curses dependency) --

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"  # CSI sequences (cursor, color, etc.)
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC sequences (title, hyperlinks, cwd)
    r"|\x1bk.*?\x1b\\"  # tmux title-setting (ESC k ... ESC \)
    r"|\x1b\[.*?m"  # SGR color codes
    r"|\x1b[()][0-9A-Za-z]"  # Character set selection
    r"|\x1b[=>]"  # Keypad modes
    r"|\x1b[\d;?]*[A-HJKfr]"  # Cursor positioning / scroll regions
    r"|\x1b\[\?[\d;]*[hl]"  # Private mode set/reset (DECSET/DECRST)
    r"|\x1b[78]"  # Save/restore cursor (DECSC/DECRC)
    r"|\x1b\[[0-9;]*~"  # Bracketed paste markers (200~/201~)
    r"|[\x00-\x07\x0e-\x1f\x7f]"  # Control chars other than backspace/tab/newline/cr
)

_CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z~])")
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
_TMUX_TITLE_RE = re.compile(r"\x1bk.*?\x1b\\")


def _render_terminal_text(text: str) -> str:
    """Render a small subset of terminal control semantics into plain text."""

    lines: list[str] = []
    current: list[str] = []
    cursor = 0
    i = 0

    def ensure_length(pos: int) -> None:
        if pos > len(current):
            current.extend(" " * (pos - len(current)))

    def write_char(ch: str) -> None:
        nonlocal cursor
        ensure_length(cursor)
        if cursor == len(current):
            current.append(ch)
        else:
            current[cursor] = ch
        cursor += 1

    def clear_to_end() -> None:
        del current[cursor:]

    while i < len(text):
        ch = text[i]

        if ch == "\n":
            lines.append("".join(current).rstrip())
            current = []
            cursor = 0
            i += 1
            continue

        if ch == "\r":
            cursor = 0
            i += 1
            continue

        if ch == "\b":
            cursor = max(0, cursor - 1)
            i += 1
            continue

        if ch == "\x1b":
            if i + 1 < len(text) and text[i + 1] in "=><()78":
                i += 2
                continue

            osc = _OSC_RE.match(text, i)
            if osc:
                i = osc.end()
                continue

            tmux_title = _TMUX_TITLE_RE.match(text, i)
            if tmux_title:
                i = tmux_title.end()
                continue

            csi = _CSI_RE.match(text, i)
            if csi:
                params, final = csi.groups()
                raw_parts = [part for part in params.lstrip("?").split(";") if part]
                parts = [int(part) if part.isdigit() else 0 for part in raw_parts]
                amount = parts[0] if parts else 1

                if final == "D":
                    cursor = max(0, cursor - amount)
                elif final == "C":
                    cursor += amount
                elif final in {"G", "`"}:
                    cursor = max(0, amount - 1)
                elif final == "K":
                    clear_to_end()
                elif final == "P":
                    del current[cursor : cursor + amount]
                i = csi.end()
                continue

            i += 1
            continue

        if ch == "\t":
            write_char("\t")
            i += 1
            continue

        if ch >= " ":
            write_char(ch)
        i += 1

    if current:
        lines.append("".join(current).rstrip())

    return "\n".join(lines)


def _strip_ansi(text: str) -> str:
    text = _render_terminal_text(text)
    text = _ANSI_RE.sub("", text)
    return re.sub(r"\[\d{3}~", "", text)


_CIRCUIT_BREAKER_LINES = 20

# -- Done-signal wrapper --


def _wrap_cmd(
    cmd: str,
    done_signal: str,
    *,
    worktree_path: str | None = None,
    headless: bool = True,
) -> str:
    """Wrap *cmd* with auto-commit and done/exit signal handling.

    Two modes controlled by *headless*:

    - **headless=True** (default, pi workers): wrapper writes ``.done`` on
      success and ``.exit`` on failure.  The wrapper is the sole signal source.
    - **headless=False** (interactive/TUI agents): wrapper only writes ``.exit``
      if ``.done`` doesn't already exist.  The agent may write ``.done`` itself
      via ``dgov worker complete``.

    When *worktree_path* is provided, auto-commits any uncommitted changes
    on both success and failure paths. The review gate judges quality, not
    the commit wrapper.
    """
    ok = shlex.quote(done_signal)
    fail = shlex.quote(done_signal + ".exit")
    auto_commit = ""
    if worktree_path:
        wt = shlex.quote(worktree_path)
        auto_commit = (
            f"git -C {wt} add -A"
            f" && {{ git -C {wt} diff --cached --quiet"
            f" || git -C {wt} commit -m 'Auto-commit on agent exit'; }}"
        )

    if headless:
        if auto_commit:
            return (
                f"{cmd}; __dgov_rc=$?; {auto_commit}; "
                f"if [ $__dgov_rc -eq 0 ]; then touch {ok}; else echo $__dgov_rc > {fail}; fi"
            )
        return f"if {cmd}; then touch {ok}; else echo $? > {fail}; fi"
    else:
        if auto_commit:
            return f"{cmd}; __rc=$?; [ -f {ok} ] || {{ {auto_commit}; echo $__rc > {fail}; }}"
        return f"{cmd}; __rc=$?; [ -f {ok} ] || echo $__rc > {fail}"


# Backward-compat aliases
def _wrap_done_signal(cmd: str, done_signal: str, *, worktree_path: str | None = None) -> str:
    return _wrap_cmd(cmd, done_signal, worktree_path=worktree_path, headless=True)


def _wrap_exit_signal(cmd: str, done_signal: str, *, worktree_path: str | None = None) -> str:
    return _wrap_cmd(cmd, done_signal, worktree_path=worktree_path, headless=False)


# -- Commit detection --


def _has_new_commits(project_root: str, branch_name: str, base_sha: str) -> bool:
    """Check if *branch_name* has commits newer than *base_sha*."""
    if not base_sha or not project_root or not branch_name:
        return False
    result = subprocess.run(
        ["git", "-C", project_root, "log", branch_name, "--not", base_sha, "--oneline"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _count_commits(project_root: str, branch: str, base_sha: str) -> int:
    """Count new commits on branch since base_sha."""
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "rev-list", "--count", f"{base_sha}..{branch}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return 0


def _has_completion_commit(pane_record: dict | None) -> bool:
    """Return True only when a pane has a real post-base commit."""
    if pane_record is None:
        return False
    project_root = pane_record.get("project_root", "")
    branch_name = pane_record.get("branch_name", "")
    base_sha = pane_record.get("base_sha", "")
    if not (project_root and branch_name and base_sha):
        return False
    return _has_new_commits(project_root, branch_name, base_sha)


# -- Agent process detection --

_AGENT_COMMANDS = frozenset(
    {
        "node",
        "pi",
        "claude",
        "codex",
        "gemini",
        "qwen",
        "python",
        "python3",
        "cursor-agent",
        "cline",
        "crush",
        "copilot",
        "amp",
        "opencode",
    }
)


def _agent_still_running(pane_id: str, current_command: str | None = None) -> bool:
    """Check if the worker's foreground process is still an agent."""
    try:
        cmd = (
            current_command
            if current_command is not None
            else get_backend().current_command(pane_id)
        )
        return cmd.strip().lower() in _AGENT_COMMANDS
    except (RuntimeError, OSError):
        return False


# -- Done detection --


def _resolve_strategy(
    done_strategy: DoneStrategy | None,
    stable_seconds: int | None,
) -> tuple[str, int]:
    """Return (strategy_type, effective_stable_seconds) from arguments.

    If an explicit DoneStrategy is provided it takes precedence.
    Otherwise fall back to the legacy stable_seconds parameter (type="signal").
    """
    if done_strategy is not None:
        stype = done_strategy.type
        if stype == "stable":
            ss = done_strategy.stable_seconds
        elif stype == "signal":
            ss = stable_seconds or 15
        else:
            ss = 0
        return stype, ss
    # No strategy provided - default to "api" (agent reports completion via dgov).
    return "api", 0


def _set_done_reason(stable_state: dict | None, reason: str) -> None:
    """Record the completion reason for the current poll."""
    if stable_state is not None:
        stable_state["_done_reason"] = reason


def _circuit_breaker_fingerprint(output: object) -> str | None:
    """Hash a normalized output tail so repeated failure states can be tracked."""
    if not isinstance(output, str) or not output:
        return None

    import hashlib

    from dgov.status import _strip_ansi

    normalized_lines = []
    for raw_line in _strip_ansi(output).splitlines():
        line = " ".join(raw_line.split())
        if line:
            normalized_lines.append(line)
    if not normalized_lines:
        return None

    window = "\n".join(normalized_lines[-_CIRCUIT_BREAKER_LINES:])
    return hashlib.sha256(window.encode()).hexdigest()[:16]


def _is_done(
    session_root: str,
    slug: str,
    pane_record: dict | None = None,
    *,
    stable_seconds: int | None = None,
    _stable_state: dict | None = None,
    done_strategy: DoneStrategy | None = None,
    alive: bool | None = None,
    current_command: str | None = None,
) -> bool:
    """Check if a worker is done via prioritized completion signals.

    Priority order is fixed and recorded in ``_stable_state["_done_reason"]``:

    1. done-signal file
    2. exit-code file
    3. commit detection (strategy-dependent)
    4. pane liveness / abandonment
    5. output stabilization (strategy-dependent)
    6. circuit breaker

    The *done_strategy* controls which optional signals are enabled:

    - **api** (default): done file -> exit file -> liveness (skip commit check).
    - **exit**: done file -> exit file -> liveness (skip commit check).
    - **commit**: done file -> exit file -> commits -> liveness (skip stabilization).
    - **stable**: done file -> exit file -> liveness -> stabilization (skip commit check).

    Returns True when the worker is no longer running (regardless of outcome).
    """
    import dgov.persistence as _persist

    if pane_record is None:
        pane_record = _persist.get_pane(session_root, slug)

    stype, eff_stable = _resolve_strategy(done_strategy, stable_seconds)

    done_path = Path(session_root, STATE_DIR, "done", slug)
    exit_path = Path(session_root, STATE_DIR, "done", slug + ".exit")

    def _complete_terminal_state(
        target_state: str,
        reason: str,
        *,
        allow_abandoned: bool = False,
        touch_done_file: bool = False,
        emit_reason: str | None = None,
    ) -> bool:
        transition = _persist.settle_completion_state(
            session_root,
            slug,
            target_state,
            allow_abandoned=allow_abandoned,
        )
        if not transition.changed:
            logger.debug(
                "late terminal signal slug=%s reason=%s current=%s target=%s",
                slug,
                reason,
                transition.state,
                target_state,
            )
            _set_done_reason(_stable_state, reason)
            return True

        logger.debug("state=%s slug=%s reason=%s", target_state, slug, reason)
        if touch_done_file:
            done_path.parent.mkdir(parents=True, exist_ok=True)
            done_path.touch()
        if emit_reason is None:
            _persist.emit_event(session_root, "pane_done", slug)
        else:
            _persist.emit_event(session_root, "pane_done", slug, reason=emit_reason)
        _set_done_reason(_stable_state, reason)
        return True

    # Signal 1a: done-signal file (clean exit) — always checked
    if done_path.exists():
        # Trust the done signal if the DB already says done (0-commit tasks)
        db_state = pane_record.get("state", "") if pane_record else ""
        if db_state == PaneState.DONE:
            _set_done_reason(_stable_state, "done_signal_db_confirmed")
            return True
        if pane_record is None or not _has_completion_commit(pane_record):
            return False

        return _complete_terminal_state(PaneState.DONE, "done_signal", allow_abandoned=True)

    # Signal 1b: exit-code file — always checked.
    # Clean exit (code 0) with commits: auto-promote to done so the governor
    # doesn't have to intervene when an agent forgets to call `dgov worker complete`.
    # Clean exit with no commits, or non-zero exit: mark failed.
    if exit_path.exists():
        exit_code = exit_path.read_text().strip()
        if exit_code == "0" and pane_record is not None:
            project_root = pane_record.get("project_root", "")
            branch_name = pane_record.get("branch_name", "")
            base_sha = pane_record.get("base_sha", "")
            if (
                project_root
                and branch_name
                and base_sha
                and _has_new_commits(project_root, branch_name, base_sha)
            ):
                return _complete_terminal_state(
                    PaneState.DONE,
                    "exit_signal_with_commits",
                    allow_abandoned=True,
                    touch_done_file=True,
                )
        return _complete_terminal_state(PaneState.FAILED, "exit_signal", allow_abandoned=True)

    if pane_record is None:
        return False

    pane_id = pane_record.get("pane_id", "")

    # Signal 2: new commits on the branch - skipped for "exit" and "stable" strategies
    if stype not in ("exit", "stable"):
        project_root = pane_record.get("project_root", "")
        branch_name = pane_record.get("branch_name", "")
        base_sha = pane_record.get("base_sha", "")
        has_commits = False
        if project_root and branch_name and base_sha:
            has_commits = _has_new_commits(project_root, branch_name, base_sha)
            logger.debug("new_commits=%s slug=%s", has_commits, slug)

        # Signal 2b: shell return without commits → failure (api strategy only)
        if stype == "api" and not has_commits and pane_id and _stable_state is not None:
            if not _agent_still_running(pane_id, current_command):
                logger.debug("shell_return_no_commits slug=%s agent exited - failed", slug)
                return _complete_terminal_state(
                    PaneState.FAILED,
                    "shell_return_no_commits",
                    allow_abandoned=True,
                )

        # api strategy without _stable_state: commit check result would be discarded
        if stype == "api" and _stable_state is None:
            pass  # skip to Signal 3
        elif has_commits:
            if stype == "api":
                # If agent is dead but shell is alive, and we have commits, we are done.
                if pane_id and not _agent_still_running(pane_id, current_command):
                    logger.debug("new_commits slug=%s agent exited - done", slug)
                    return _complete_terminal_state(
                        PaneState.DONE,
                        "commit",
                        allow_abandoned=True,
                        touch_done_file=True,
                    )
                elif _stable_state is not None:
                    _stable_state["commits_detected"] = True
                    return False
                else:
                    return False
            else:
                if pane_id and _agent_still_running(pane_id, current_command):
                    # Agent committed but is still running - grace period
                    if _stable_state is not None:
                        commit_count = _count_commits(project_root, branch_name, base_sha)
                        prev_count = _stable_state.get("commit_count")
                        if prev_count is None or commit_count != prev_count:
                            # New commits - reset grace timer
                            _stable_state["commit_count"] = commit_count
                            _stable_state["commit_seen_at"] = time.monotonic()
                            logger.debug(
                                "new_commits slug=%s count=%d agent running, grace",
                                slug,
                                commit_count,
                            )
                            return False
                        commit_seen = _stable_state.get("commit_seen_at", 0)
                        elapsed = time.monotonic() - commit_seen
                        if elapsed < 30:
                            logger.debug(
                                "new_commits slug=%s grace period %.0fs/30s", slug, elapsed
                            )
                            return False
                        logger.debug(
                            "new_commits slug=%s grace period elapsed, declaring done",
                            slug,
                        )
                        # Fall through to done
                    else:
                        logger.warning(
                            "new_commits slug=%s agent running, no stable_state - done",
                            slug,
                        )
                        # Fall through - blocking forever is worse
                return _complete_terminal_state(
                    PaneState.DONE,
                    "commit",
                    allow_abandoned=True,
                    touch_done_file=True,
                )

    # Signal 3: pane no longer alive with no done file and no commits → abandoned
    if pane_id:
        if alive is None:
            alive = get_backend().is_alive(pane_id)
        logger.debug("pane alive=%s slug=%s", alive, slug)
        if not alive:
            # Grace period: only declare abandoned after 10s dead
            if _stable_state is not None:
                dead_since = _stable_state.get("dead_since")
                if dead_since is None:
                    _stable_state["dead_since"] = time.monotonic()
                    logger.debug("pane first seen dead slug=%s", slug)
                elif time.monotonic() - dead_since >= 10:
                    transition = _persist.settle_completion_state(
                        session_root,
                        slug,
                        PaneState.ABANDONED,
                    )
                    if transition.changed:
                        _persist.emit_event(session_root, "pane_done", slug)
                    _set_done_reason(_stable_state, "abandoned")
                    return True
            else:
                transition = _persist.settle_completion_state(
                    session_root,
                    slug,
                    PaneState.ABANDONED,
                )
                if transition.changed:
                    _persist.emit_event(session_root, "pane_done", slug)
                _set_done_reason(_stable_state, "abandoned")
                return True
        elif _stable_state is not None:
            # Pane came back alive - reset dead tracking
            _stable_state.pop("dead_since", None)

    # API strategy: signal files + liveness, with commit+stable fallback for interactive agents
    if stype == "api":
        # Run output stability tracking for fallback (api skips Signal 4)
        if _stable_state is not None and pane_id:
            current_output = _stable_state.pop("current_output", None)
            if current_output is None:
                from dgov.status import capture_worker_output

                project_root = pane_record.get("project_root", "")
                current_output = capture_worker_output(
                    project_root, slug, lines=20, session_root=session_root
                )
            if isinstance(current_output, str):
                last_output = _stable_state.get("last_output")
                if current_output == last_output:
                    if _stable_state.get("stable_since") is None:
                        _stable_state["stable_since"] = time.monotonic()
                else:
                    _stable_state["last_output"] = current_output
                    _stable_state["stable_since"] = None

        # Fallback: if agent has commits and pane output has been stable for 60s, treat as done
        if _stable_state is not None and _stable_state.get("commits_detected"):
            stable_since = _stable_state.get("stable_since")
            elapsed = time.monotonic() - stable_since if stable_since is not None else 0
            if elapsed > 60:
                logger.info(
                    "api fallback: %s has commits and stable output for 60s, marking done", slug
                )
                return _complete_terminal_state(
                    PaneState.DONE,
                    "api_fallback_stable",
                    allow_abandoned=True,
                    touch_done_file=True,
                    emit_reason="api_fallback_stable",
                )
        return False

    # Signal 4 (optional): output stabilization - skipped for "commit" strategy
    use_stable = stype == "stable" or (stype == "signal" and eff_stable > 0)
    if use_stable and stype != "commit" and _stable_state is not None and pane_id:
        current_output = _stable_state.pop("current_output", None)
        if current_output is None:
            from dgov.status import capture_worker_output

            project_root = pane_record.get("project_root", "")
            current_output = capture_worker_output(
                project_root, slug, lines=20, session_root=session_root
            )
        if isinstance(current_output, str):
            last_output = _stable_state.get("last_output")
            stable_since = _stable_state.get("stable_since")
            if current_output == last_output:
                if stable_since is None:
                    _stable_state["stable_since"] = time.monotonic()
                elif time.monotonic() - stable_since >= eff_stable:
                    if _agent_still_running(pane_id, current_command):
                        _stable_state["stable_since"] = None
                    else:
                        if not _has_completion_commit(pane_record):
                            return False
                        return _complete_terminal_state(
                            PaneState.DONE,
                            "stable",
                            touch_done_file=True,
                        )
            else:
                _stable_state["last_output"] = current_output
                _stable_state["stable_since"] = None

    # Signal 5: Circuit breaker - detect stuck workers repeating same output
    if _stable_state is not None and pane_id:
        output_hash = _circuit_breaker_fingerprint(
            _stable_state.get("current_output") or _stable_state.get("last_output", "")
        )
        if output_hash:
            prev_hash = _stable_state.get("_cb_prev_hash")
            if output_hash != prev_hash:
                count = _persist.record_failure(session_root, slug, output_hash)
                if count >= _persist.CIRCUIT_BREAKER_THRESHOLD:
                    logger.info(
                        "circuit_breaker slug=%s hash=%s count=%d",
                        slug,
                        output_hash,
                        count,
                    )
                    transition = _persist.settle_completion_state(
                        session_root,
                        slug,
                        PaneState.FAILED,
                    )
                    if transition.changed:
                        # Derived from event — no stored field.
                        _persist.emit_event(session_root, "pane_circuit_breaker", slug)
                    _set_done_reason(_stable_state, "circuit_breaker")
                    return True
            _stable_state["_cb_prev_hash"] = output_hash

    return False
