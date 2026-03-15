"""Wait/poll logic for worker panes."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.backend import get_backend
from dgov.persistence import STATE_DIR

if TYPE_CHECKING:
    from dgov.agents import DoneStrategy

logger = logging.getLogger(__name__)

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


# -- Blocked / question detection --

_BLOCKED_PATTERNS = [
    re.compile(r"(?i)do you want to proceed"),
    re.compile(r"(?i)proceed\?"),
    re.compile(r"\by/n\b", re.IGNORECASE),
    re.compile(r"\bY/N\b"),
    re.compile(r"\[yes/no\]", re.IGNORECASE),
    re.compile(r"(?i)are you sure"),
    re.compile(r"(?i)enter password"),
    re.compile(r"(?i)enter passphrase"),
    re.compile(r"(?i)permission denied"),
]


def _detect_blocked(output: str) -> str | None:
    """Check captured output for question/prompt patterns.

    Returns the matched text or None.
    """
    if not output:
        logger.debug("blocked_check slug=N/A reason=no_output")
        return None
    # Only scan the last 10 lines to avoid false positives from old output
    lines = output.strip().splitlines()[-10:]
    tail = "\n".join(lines)
    for pattern in _BLOCKED_PATTERNS:
        m = pattern.search(tail)
        if m:
            logger.debug("blocked slug matched_pattern=%s", m.re.pattern[:40])
            return m.group(0)
    logger.debug("blocked_check slug=N/A reason=no_match")
    return None


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
        ss = done_strategy.stable_seconds if stype == "stable" else (stable_seconds or 15)
        return stype, ss
    return "signal", stable_seconds or 0


def _is_done(
    session_root: str,
    slug: str,
    pane_record: dict | None = None,
    *,
    stable_seconds: int | None = None,
    _stable_state: dict | None = None,
    done_strategy: DoneStrategy | None = None,
) -> bool:
    """Check if a worker is done via any of four signals.

    The *done_strategy* controls which signals are primary vs fallback:

    - **signal** (default): done file → exit file → commits → liveness → stabilization.
    - **exit**: done file → exit file → liveness (skip commit check).
    - **commit**: done file → exit file → commits → liveness (skip stabilization).
    - **stable**: done file → exit file → liveness → stabilization (skip commit check).

    All strategies always check the done/exit files first (they're authoritative).

    Returns True when the worker is no longer running (regardless of outcome).
    """
    import dgov.persistence as _persist

    stype, eff_stable = _resolve_strategy(done_strategy, stable_seconds)

    done_path = Path(session_root, STATE_DIR, "done", slug)
    exit_path = Path(session_root, STATE_DIR, "done", slug + ".exit")

    # Signal 1a: done-signal file (clean exit) — always checked
    if done_path.exists():
        current_state = pane_record.get("state", "") if pane_record else ""
        force = current_state == "abandoned"
        logger.debug("state=%s slug=%s reason=done_signal", "done", slug)
        _persist.update_pane_state(session_root, slug, "done", force=force)
        return True

    # Signal 1b: exit-code file (agent crashed / nonzero exit) — always checked
    if exit_path.exists():
        current_state = pane_record.get("state", "") if pane_record else ""
        force = current_state == "abandoned"
        logger.debug("state=%s slug=%s reason=exit_signal", "failed", slug)
        _persist.update_pane_state(session_root, slug, "failed", force=force)
        return True

    if pane_record is None:
        return False

    pane_id = pane_record.get("pane_id", "")

    # Signal 2: new commits on the branch — skipped for "exit" and "stable" strategies
    if stype not in ("exit", "stable"):
        project_root = pane_record.get("project_root", "")
        branch_name = pane_record.get("branch_name", "")
        base_sha = pane_record.get("base_sha", "")
        if project_root and branch_name and base_sha:
            has_commits = _has_new_commits(project_root, branch_name, base_sha)
            logger.debug("new_commits=%s slug=%s", has_commits, slug)
            if has_commits:
                if pane_id and _agent_still_running(pane_id):
                    # Agent committed but is still running — grace period
                    if _stable_state is not None:
                        commit_count = _count_commits(project_root, branch_name, base_sha)
                        prev_count = _stable_state.get("commit_count")
                        if prev_count is None or commit_count != prev_count:
                            # New commits — reset grace timer
                            _stable_state["commit_count"] = commit_count
                            _stable_state["commit_seen_at"] = time.monotonic()
                            logger.debug(
                                "new_commits slug=%s count=%d agent still running, starting grace",
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
                            "new_commits slug=%s grace period elapsed, declaring done", slug
                        )
                        # Fall through to done
                    else:
                        logger.debug(
                            "new_commits slug=%s but agent still running (no stable_state)", slug
                        )
                        return False
                current_state = pane_record.get("state", "")
                force = current_state == "abandoned"
                _persist.update_pane_state(session_root, slug, "done", force=force)
                _persist.emit_event(session_root, "pane_done", slug)
                # Touch done-signal so we don't re-emit
                done_path.parent.mkdir(parents=True, exist_ok=True)
                done_path.touch()
                return True

    # Signal 3: pane no longer alive with no done file and no commits → abandoned
    if pane_id:
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
                    _persist.update_pane_state(session_root, slug, "abandoned")
                    _persist.emit_event(session_root, "pane_done", slug)
                    return True
            else:
                _persist.update_pane_state(session_root, slug, "abandoned")
                _persist.emit_event(session_root, "pane_done", slug)
                return True
        elif _stable_state is not None:
            # Pane came back alive — reset dead tracking
            _stable_state.pop("dead_since", None)

    # Signal 4 (optional): output stabilization — skipped for "commit" strategy
    use_stable = stype == "stable" or (stype == "signal" and eff_stable > 0)
    if use_stable and stype != "commit" and _stable_state is not None and pane_id:
        from dgov.status import capture_worker_output

        project_root = pane_record.get("project_root", "")
        current_output = capture_worker_output(
            project_root, slug, lines=20, session_root=session_root
        )
        if current_output is not None:
            last_output = _stable_state.get("last_output")
            stable_since = _stable_state.get("stable_since")
            if current_output == last_output:
                if stable_since is None:
                    _stable_state["stable_since"] = time.monotonic()
                elif time.monotonic() - stable_since >= eff_stable:
                    if _agent_still_running(pane_id):
                        _stable_state["stable_since"] = None
                    else:
                        done_path.parent.mkdir(parents=True, exist_ok=True)
                        done_path.touch()
                        _persist.update_pane_state(session_root, slug, "done")
                        return True
            else:
                _stable_state["last_output"] = current_output
                _stable_state["stable_since"] = None

    return False


# -- Agent process detection --

# Known foreground commands that indicate an agent is still active
_AGENT_COMMANDS = frozenset(
    {"node", "pi", "claude", "codex", "gemini", "qwen", "python", "python3"}
)


def _agent_still_running(pane_id: str) -> bool:
    """Check if the worker's foreground process is still an agent."""
    try:
        cmd = get_backend().current_command(pane_id)
        return cmd.strip().lower() in _AGENT_COMMANDS
    except (RuntimeError, OSError):
        return False


def _strategy_for_pane(pane_record: dict | None) -> DoneStrategy | None:
    """Look up the DoneStrategy for a pane's agent, if any."""
    if pane_record is None:
        return None
    agent_id = pane_record.get("agent")
    if not agent_id:
        return None
    from dgov.agents import AGENT_REGISTRY, load_registry

    registry = AGENT_REGISTRY
    agent_def = registry.get(agent_id)
    if agent_def is None:
        # Might be a TOML-defined agent — try loading the full registry
        registry = load_registry(pane_record.get("project_root"))
        agent_def = registry.get(agent_id)
    return agent_def.done_strategy if agent_def else None


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
    last_blocked: str | None = None,
    done_strategy: DoneStrategy | None = None,
) -> tuple[bool, str, str | None, float | None, str | None]:
    """Single poll cycle shared by wait_worker_pane and wait_all_worker_panes.

    Returns (is_done, method, last_output, stable_since, last_blocked).
    """
    import dgov.persistence as _persist

    logger.debug("poll slug=%s", slug)

    # Build stabilization state dict for unified _is_done
    stable_state: dict = {
        "last_output": last_output,
        "stable_since": stable_since,
        "last_blocked": last_blocked,
    }

    if _is_done(
        session_root,
        slug,
        pane_record=pane_record,
        stable_seconds=stable,
        _stable_state=stable_state,
        done_strategy=done_strategy,
    ):
        # Determine method: check if done file existed before we called _is_done
        done_path = Path(session_root, STATE_DIR, "done", slug)
        if done_path.exists():
            # Could be signal, commit, or stable — check stable_state to distinguish
            if stable_state.get("stable_since") is not None:
                return (
                    True,
                    "stable",
                    stable_state.get("last_output"),
                    stable_state.get("stable_since"),
                    stable_state.get("last_blocked"),
                )
            return (
                True,
                "signal_or_commit",
                stable_state.get("last_output"),
                stable_state.get("stable_since"),
                stable_state.get("last_blocked"),
            )
        return (
            True,
            "signal_or_commit",
            stable_state.get("last_output"),
            stable_state.get("stable_since"),
            stable_state.get("last_blocked"),
        )

    # Check for blocked state and auto-respond if possible
    current_output = stable_state.get("last_output")
    if current_output:
        blocked_match = _detect_blocked(current_output)
        if blocked_match:
            from dgov.responder import auto_respond

            rule = auto_respond(session_root, slug, current_output)
            if rule is None:
                # Only emit if this is a new blocked match
                last_blocked = stable_state.get("last_blocked")
                if blocked_match != last_blocked:
                    stable_state["last_blocked"] = blocked_match
                    _persist.emit_event(session_root, "pane_blocked", slug, question=blocked_match)

    return (
        False,
        "",
        stable_state.get("last_output"),
        stable_state.get("stable_since"),
        stable_state.get("last_blocked"),
    )


# -- Public wait API --


def wait_for_slugs(
    session_root: str,
    slugs: list[str],
    timeout: int = 600,
    poll: int = 3,
    stable_seconds: int | None = None,
) -> set[str]:
    """Wait for a set of slugs to finish. Returns the set of slugs still pending at timeout."""
    import dgov.persistence as _persist

    start = time.monotonic()
    pending = set(slugs)
    stable_states: dict[str, dict] = {s: {} for s in slugs}
    strategies: dict[str, DoneStrategy | None] = {}
    while pending and (time.monotonic() - start < timeout):
        for slug in list(pending):
            rec = _persist.get_pane(session_root, slug)
            if slug not in strategies:
                strategies[slug] = _strategy_for_pane(rec)
            if _is_done(
                session_root,
                slug,
                pane_record=rec,
                stable_seconds=stable_seconds,
                _stable_state=stable_states[slug],
                done_strategy=strategies[slug],
            ):
                pending.discard(slug)
        if pending:
            time.sleep(poll)
    return pending


def wait_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
    auto_retry: bool = True,
) -> dict:
    """Wait for a single worker pane to finish.

    Returns ``{"done": slug, "method": ...}`` on success.
    Raises ``PaneTimeoutError`` on timeout.

    When *auto_retry* is True and the pane ends in "failed" or "abandoned"
    state, consults the agent's retry policy and may automatically retry
    or escalate.
    """
    import dgov.persistence as _persist

    logger.debug("wait_for_pane slug=%s timeout=%ds", slug, timeout)
    session_root = os.path.abspath(session_root or project_root)
    pane_record = _persist.get_pane(session_root, slug)
    strategy = _strategy_for_pane(pane_record)
    start = time.monotonic()
    last_output: str | None = None
    stable_since: float | None = None
    last_blocked: str | None = None

    while True:
        done, method, last_output, stable_since, last_blocked = _poll_once(
            session_root,
            project_root,
            slug,
            pane_record,
            last_output,
            stable_since,
            stable,
            last_blocked,
            done_strategy=strategy,
        )
        if done:
            # Check if it failed and we should auto-retry
            rec = _persist.get_pane(session_root, slug)
            current_state = rec.get("state", "") if rec else ""

            if auto_retry and current_state in ("failed", "abandoned"):
                from dgov.retry import maybe_auto_retry

                retry_result = maybe_auto_retry(session_root, slug, project_root)
                if retry_result:
                    new_slug = retry_result.get("new_slug", "")
                    if new_slug:
                        # Continue waiting on the new pane
                        slug = new_slug
                        pane_record = _persist.get_pane(session_root, slug)
                        strategy = _strategy_for_pane(pane_record)
                        last_output = None
                        stable_since = None
                        last_blocked = None
                        continue

            elapsed = time.monotonic() - start
            logger.debug("wait completed slug=%s state=%s duration=%.1fs", slug, method, elapsed)
            return {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            logger.warning("wait timed out slug=%s after=%.1fs", slug, elapsed)
            _persist.update_pane_state(session_root, slug, "timed_out")
            _persist.emit_event(session_root, "pane_timed_out", slug)
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
    import dgov.persistence as _persist
    from dgov.status import list_worker_panes

    session_root = os.path.abspath(session_root or project_root)
    panes = list_worker_panes(project_root, session_root=session_root)
    pending = {p["slug"] for p in panes if not p["done"]}
    if not pending:
        return

    start = time.monotonic()
    stable_trackers: dict[str, tuple[str | None, float | None, str | None]] = {
        s: (None, None, None) for s in pending
    }
    strategies: dict[str, DoneStrategy | None] = {}

    while pending:
        for slug in list(pending):
            rec = _persist.get_pane(session_root, slug)
            if slug not in strategies:
                strategies[slug] = _strategy_for_pane(rec)
            last, since, blocked = stable_trackers.get(slug, (None, None, None))
            done, method, last, since, blocked = _poll_once(
                session_root,
                project_root,
                slug,
                rec,
                last,
                since,
                stable,
                blocked,
                done_strategy=strategies[slug],
            )
            stable_trackers[slug] = (last, since, blocked)
            if done:
                pending.discard(slug)
                yield {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            pending_info = []
            for s in sorted(pending):
                rec = _persist.get_pane(session_root, s)
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


# -- Communication helpers --


def interact_with_pane(session_root: str, slug: str, message: str) -> bool:
    """Send a message to a worker pane.

    Returns True if the message was sent, False if the pane wasn't found or dead.
    """
    import dgov.persistence as _persist

    target = _persist.get_pane(session_root, slug)
    if not target:
        return False

    pane_id = target.get("pane_id", "")
    if not pane_id or not get_backend().is_alive(pane_id):
        return False

    get_backend().send_input(pane_id, message)
    return True


def nudge_pane(session_root: str, slug: str, wait_seconds: int = 10) -> dict:
    """Send 'are you done?' to a worker and parse the response.

    Sends the nudge, waits *wait_seconds*, captures output, and looks for
    YES or NO. If YES, touches the done-signal file.

    Returns {"response": "YES"|"NO"|"unclear", "output": str}.
    """
    import dgov.persistence as _persist

    target = _persist.get_pane(session_root, slug)
    if not target:
        return {"response": "error", "output": "Pane not found"}

    pane_id = target.get("pane_id", "")
    if not pane_id or not get_backend().is_alive(pane_id):
        return {"response": "error", "output": "Pane dead"}

    # Send the nudge
    get_backend().send_input(pane_id, "Are you done? Reply YES or NO.")
    time.sleep(wait_seconds)

    # Capture output
    captured = get_backend().capture_output(pane_id, lines=15)

    # Parse for YES/NO
    response = "unclear"
    if captured:
        lines = captured.strip().splitlines()
        for line in reversed(lines):
            upper = line.strip().upper()
            if "YES" in upper:
                response = "YES"
                break
            if "NO" in upper:
                response = "NO"
                break

    if response == "YES":
        done_path = Path(session_root) / STATE_DIR / "done" / slug
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.touch()
        _persist.update_pane_state(session_root, slug, "done")

    return {"response": response, "output": captured or ""}


def signal_pane(session_root: str, slug: str, signal: str) -> bool:
    """Manually signal a pane as done or failed.

    Touches the appropriate signal file and updates state.
    Returns True on success, False if pane not found.
    """
    import dgov.persistence as _persist

    target = _persist.get_pane(session_root, slug)
    if not target:
        return False

    done_dir = Path(session_root) / STATE_DIR / "done"
    done_dir.mkdir(parents=True, exist_ok=True)

    if signal == "done":
        (done_dir / slug).touch()
        _persist.update_pane_state(session_root, slug, "done")
    elif signal == "failed":
        (done_dir / f"{slug}.exit").write_text("manual")
        _persist.update_pane_state(session_root, slug, "failed")
    else:
        raise ValueError(f"Unknown signal: {signal!r}. Must be 'done' or 'failed'.")

    return True
