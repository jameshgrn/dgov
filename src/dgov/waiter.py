"""Wait/poll logic for worker panes."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from dgov import tmux
from dgov.persistence import _STATE_DIR

# -- Done-signal wrapper --


def _wrap_done_signal(cmd: str, done_signal: str) -> str:
    """Wrap *cmd* so done-signal is only touched on success."""
    ok = shlex.quote(done_signal)
    fail = shlex.quote(done_signal + ".exit")
    return f"if {cmd}; then touch {ok}; else echo $? > {fail}; fi"


# -- Commit detection --


def _has_new_commits(project_root: str, branch_name: str, base_sha: str) -> bool:
    """Check if *branch_name* has commits newer than *base_sha*."""
    if not base_sha:
        return False
    result = subprocess.run(
        ["git", "-C", project_root, "log", branch_name, "--not", base_sha, "--oneline"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


# -- Done detection --


def _is_done(session_root: str, slug: str, pane_record: dict | None = None) -> bool:
    """Check if a worker is done via any of four signals.

    1a. Done-signal file exists (agent exited cleanly) → state "done".
    1b. Exit-code file exists (agent exited nonzero) → state "failed".
    2.  Branch has new commits beyond base_sha → state "done".
    3.  Pane is no longer alive with no done file and no commits → state "abandoned".

    Returns True when the worker is no longer running (regardless of outcome).
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    done_path = Path(session_root, _STATE_DIR, "done", slug)
    exit_path = Path(session_root, _STATE_DIR, "done", slug + ".exit")

    # Signal 1a: done-signal file (clean exit)
    if done_path.exists():
        _p._update_pane_state(session_root, slug, "done")
        return True

    # Signal 1b: exit-code file (agent crashed / nonzero exit)
    if exit_path.exists():
        _p._update_pane_state(session_root, slug, "failed")
        return True

    if pane_record is None:
        return False

    # Signal 2: new commits on the branch
    project_root = pane_record.get("project_root", "")
    branch_name = pane_record.get("branch_name", "")
    base_sha = pane_record.get("base_sha", "")
    if project_root and branch_name and base_sha:
        if _p._has_new_commits(project_root, branch_name, base_sha):
            _p._update_pane_state(session_root, slug, "done")
            _p._emit_event(session_root, "pane_done", slug)
            # Touch done-signal so we don't re-emit
            done_path.parent.mkdir(parents=True, exist_ok=True)
            done_path.touch()
            return True

    # Signal 3: pane no longer alive with no done file and no commits → abandoned
    pane_id = pane_record.get("pane_id", "")
    if pane_id and not tmux.pane_exists(pane_id):
        _p._update_pane_state(session_root, slug, "abandoned")
        _p._emit_event(session_root, "pane_done", slug)
        return True

    return False


# -- Agent process detection --

# Known foreground commands that indicate an agent is still active
_AGENT_COMMANDS = frozenset(
    {"node", "pi", "claude", "codex", "gemini", "qwen", "python", "python3"}
)


def _agent_still_running(pane_id: str) -> bool:
    """Check if the tmux pane's foreground process is still an agent."""
    try:
        cmd = tmux.current_command(pane_id)
        return cmd.strip().lower() in _AGENT_COMMANDS
    except (RuntimeError, OSError):
        return False


# -- Timeout error --


class PaneTimeoutError(Exception):
    """Raised when waiting for a pane exceeds the timeout."""

    def __init__(
        self,
        slug: str,
        timeout: int,
        agent: str = "unknown",
        pending_panes: list[dict] | None = None,
    ):
        self.slug = slug
        self.timeout = timeout
        self.agent = agent
        self.pending_panes = pending_panes or [{"slug": slug, "agent": agent}]
        super().__init__(f"Pane {slug!r} timed out after {timeout}s")


# -- Poll --


def _poll_once(
    session_root: str,
    project_root: str,
    slug: str,
    pane_record: dict | None,
    last_output: str | None,
    stable_since: float | None,
    stable: int,
) -> tuple[bool, str, str | None, float | None]:
    """Single poll cycle shared by wait_worker_pane and wait_all_worker_panes.

    Returns (is_done, method, last_output, stable_since).
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    if _p._is_done(session_root, slug, pane_record=pane_record):
        return True, "signal_or_commit", last_output, stable_since

    current_output = _p.capture_worker_output(
        project_root, slug, lines=20, session_root=session_root
    )
    if current_output is not None:
        if current_output == last_output:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable:
                # Check if agent process is still running — if so, it's thinking, not done
                pane_id = pane_record.get("pane_id", "") if pane_record else ""
                if pane_id and _p._agent_still_running(pane_id):
                    stable_since = None  # Reset — agent is alive, just quiet
                else:
                    done_path = Path(session_root) / _STATE_DIR / "done" / slug
                    done_path.parent.mkdir(parents=True, exist_ok=True)
                    done_path.touch()
                    return True, "stable", current_output, stable_since
        else:
            last_output = current_output
            stable_since = None

    return False, "", last_output, stable_since


# -- Public wait API --


def wait_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
) -> dict:
    """Wait for a single worker pane to finish.

    Returns ``{"done": slug, "method": ...}`` on success.
    Raises ``PaneTimeoutError`` on timeout.
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    session_root = os.path.abspath(session_root or project_root)
    pane_record = _p._get_pane(session_root, slug)
    start = time.monotonic()
    last_output: str | None = None
    stable_since: float | None = None

    while True:
        done, method, last_output, stable_since = _poll_once(
            session_root,
            project_root,
            slug,
            pane_record,
            last_output,
            stable_since,
            stable,
        )
        if done:
            _p._update_pane_state(session_root, slug, "done")
            return {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            _p._update_pane_state(session_root, slug, "timed_out")
            _p._emit_event(session_root, "pane_timed_out", slug)
            agent = pane_record.get("agent", "unknown") if pane_record else "unknown"
            raise PaneTimeoutError(slug, timeout, agent)
        time.sleep(poll)


def wait_all_worker_panes(
    project_root: str,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
):
    """Wait for ALL worker panes to finish.

    Yields ``{"done": slug, "method": ...}`` as each pane completes.
    Raises ``PaneTimeoutError`` (with the first timed-out slug) on timeout.
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    session_root = os.path.abspath(session_root or project_root)
    panes = _p.list_worker_panes(project_root, session_root=session_root)
    pending = {p["slug"] for p in panes if not p["done"]}
    if not pending:
        return

    start = time.monotonic()
    stable_trackers: dict[str, tuple[str | None, float | None]] = {
        s: (None, None) for s in pending
    }

    while pending:
        for slug in list(pending):
            rec = _p._get_pane(session_root, slug)
            last, since = stable_trackers.get(slug, (None, None))
            done, method, last, since = _poll_once(
                session_root,
                project_root,
                slug,
                rec,
                last,
                since,
                stable,
            )
            stable_trackers[slug] = (last, since)
            if done:
                _p._update_pane_state(session_root, slug, "done")
                pending.discard(slug)
                yield {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            pending_info = []
            for s in sorted(pending):
                rec = _p._get_pane(session_root, s)
                pending_info.append(
                    {
                        "slug": s,
                        "agent": rec.get("agent", "unknown") if rec else "unknown",
                    }
                )
            first = pending_info[0]
            raise PaneTimeoutError(
                first["slug"],
                timeout,
                first["agent"],
                pending_panes=pending_info,
            )
        if pending:
            time.sleep(poll)
