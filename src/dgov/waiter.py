"""Wait/poll logic for worker panes."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.backend import get_backend
from dgov.done import (  # noqa: F401 — re-export for backwards compat
    _agent_still_running,
    _count_commits,
    _has_new_commits,
    _is_done,
    _resolve_strategy,
    _wrap_done_signal,
)
from dgov.persistence import STATE_DIR

if TYPE_CHECKING:
    from dgov.agents import DoneStrategy

logger = logging.getLogger(__name__)

# Track which slugs have received an auto-nudge (module lifetime)
_nudged_slugs: set[str] = set()


# -- Blocked / question detection --

_BLOCKED_PATTERNS = [
    re.compile(r"(?i)do you want to proceed"),
    re.compile(r"(?i)proceed\?"),
    re.compile(r"\by/n\b", re.IGNORECASE),
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
    stable_state: dict,
    stable: int,
    done_strategy: DoneStrategy | None = None,
    alive: bool | None = None,
) -> tuple[bool, str]:
    """Single poll cycle shared by wait_worker_pane and wait_all_worker_panes.

    Returns (is_done, method).
    """
    import dgov.persistence as _persist

    logger.debug("poll slug=%s", slug)

    stable_state["current_output"] = None

    pane_id = pane_record.get("pane_id", "") if pane_record else ""
    if pane_id:
        if alive is None:
            alive = get_backend().is_alive(pane_id)
        stable_state["current_output"] = (
            get_backend().capture_output(pane_id, lines=20) if alive else ""
        )
        _current_cmd = get_backend().current_command(pane_id) if alive else None
    else:
        _current_cmd = None

    if _is_done(
        session_root,
        slug,
        pane_record=pane_record,
        stable_seconds=stable,
        _stable_state=stable_state,
        done_strategy=done_strategy,
        alive=alive,
        current_command=_current_cmd,
    ):
        method = stable_state.get("_done_reason", "signal_or_commit")
        return (True, method)

    # Auto-nudge: api strategy panes stable 45s+ without done signal get a reminder
    if done_strategy is not None and done_strategy.type == "api" and slug not in _nudged_slugs:
        nudge_stable = stable_state.get("stable_since")
        if nudge_stable is not None and time.monotonic() - nudge_stable > 45:
            _pane_id = pane_record.get("pane_id", "") if pane_record else ""
            if (
                _pane_id
                and get_backend().is_alive(_pane_id)
                and _agent_still_running(_pane_id, _current_cmd)
            ):
                get_backend().send_input(
                    _pane_id,
                    "REMINDER: commit your work and signal completion: "
                    "git add <files> && git commit -m 'message' && "
                    "dgov worker complete -m 'summary'",
                )
                _nudged_slugs.add(slug)
                logger.info("auto_nudge slug=%s after 45s stable", slug)

    # Check for blocked state and auto-respond if possible
    current_output = stable_state.get("current_output")
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

    return (False, "")


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
            pane_id = rec.get("pane_id", "") if rec else ""
            alive = None
            if pane_id:
                alive = get_backend().is_alive(pane_id)
                stable_states[slug]["current_output"] = (
                    get_backend().capture_output(pane_id, lines=20) if alive else None
                )
            if _is_done(
                session_root,
                slug,
                pane_record=rec,
                stable_seconds=stable_seconds,
                _stable_state=stable_states[slug],
                done_strategy=strategies[slug],
                alive=alive,
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
    stable_state: dict = {}

    while True:
        done, method = _poll_once(
            session_root,
            project_root,
            slug,
            pane_record,
            stable_state,
            stable,
            done_strategy=strategy,
        )
        if done:
            # Check if it failed and we should auto-retry
            rec = _persist.get_pane(session_root, slug)
            current_state = rec.get("state", "") if rec else ""

            if auto_retry and current_state in ("failed", "abandoned"):
                from dgov.recovery import maybe_auto_retry

                retry_result = maybe_auto_retry(session_root, slug, project_root)
                if retry_result:
                    new_slug = retry_result.get("new_slug", "")
                    if new_slug:
                        # Continue waiting on the new pane
                        slug = new_slug
                        pane_record = _persist.get_pane(session_root, slug)
                        strategy = _strategy_for_pane(pane_record)
                        stable_state = {}
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
    stable_states: dict[str, dict] = {s: {} for s in pending}
    strategies: dict[str, DoneStrategy | None] = {}

    while pending:
        # One bulk tmux call per tick — avoids N individual is_alive forks
        alive_panes = set(get_backend().bulk_info().keys())

        for slug in list(pending):
            rec = _persist.get_pane(session_root, slug)
            if slug not in strategies:
                strategies[slug] = _strategy_for_pane(rec)
            ss = stable_states.setdefault(slug, {})
            pane_id = rec.get("pane_id", "") if rec else ""
            done, method = _poll_once(
                session_root,
                project_root,
                slug,
                rec,
                ss,
                stable,
                done_strategy=strategies[slug],
                alive=pane_id in alive_panes if pane_id else None,
            )
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

    if not get_backend().is_alive(pane_id):
        return {"response": "error", "output": "Pane died during wait"}

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
