"""Lightweight polling daemon for worker state classification and auto-remediation.

Uses local Qwen 4B (localhost:8082) to classify worker outputs and
take automated actions (auto-complete, nudge, idle timeout).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.agents import load_registry
from dgov.backend import get_backend
from dgov.done import _has_new_commits
from dgov.monitor_hooks import load_monitor_hooks, match_monitor_hook
from dgov.openrouter import chat_completion_local_first
from dgov.persistence import (
    STATE_DIR,
    emit_event,
    get_pane,
    set_pane_metadata,
    update_pane_state,
)
from dgov.status import list_worker_panes, prune_stale_panes, tail_worker_log

if TYPE_CHECKING:
    from dgov.monitor_hooks import MonitorHook

logger = logging.getLogger(__name__)


# Deterministic regex patterns for classification
DETERMINISTIC_PATTERNS = {
    "stuck": [
        r"\b(failed|error|exception|traceback|crash|panic|fatal)\b",
    ],
    "waiting_input": [
        r"\b(waiting[ \t]+for\s+(user|input|confirmation|approval|prompt))\b",
        r"\b(paused.*awaiting\b|awaiting.*input\b)",
        r"\bawaiting\s+input\b",
    ],
    "done": [
        r"\b(done|complete[d]?|finish[e]?d?|\bready\b|success|all\.done)",
        r"[\u279c\u2714]\s+[\w\-\.]+\s+git:\([\w\-\.]+\)",  # shell prompt on branch
    ],
    "committing": [
        r"\b(commit|git\s+add|git\s+commit|pushing|pushed|committed)\b",
    ],
    "idle": [
        r"\b(no[ \t]+work|pause[d]?|idling)\b",
    ],
}


def _classify_deterministic(output: str) -> str | None:
    """Try to classify output using deterministic regex patterns first.

    Returns classification if matched, otherwise None for LLM fallback.
    Called before LLM classification to avoid unnecessary API calls.
    """
    output_lower = output.lower()

    for state, patterns in DETERMINISTIC_PATTERNS.items():
        for pattern in patterns:
            if _regex_match(pattern, output_lower):
                return state

    return None


def _regex_match(pattern: str, text: str) -> bool:
    """Match a regex pattern against text. Input is already lowercased."""

    try:
        return re.search(pattern, text) is not None
    except re.error:
        logger.debug("Invalid regex pattern: %s", pattern)
        return False


def classify_output(
    output: str, hooks: list[MonitorHook] | None = None
) -> str | tuple[str, MonitorHook]:
    """Classify agent output into working, done, stuck, idle, waiting_input, or committing.

    Uses configurable hooks first, then deterministic regex patterns, and finally
    falls back to LLM for ambiguous cases.
    """
    if not output.strip():
        return "idle"

    # Layer 0: Monitor Hooks (user-configured overrides)
    if hooks:
        matching_hook = match_monitor_hook(output, hooks)
        if matching_hook:
            logger.debug(
                "Monitor hook matched: %s (%s)", matching_hook.kind, matching_hook.pattern
            )
            return ("hook_match", matching_hook)

    # Layer 1: Deterministic classification (fast, no API call)
    deterministic_result = _classify_deterministic(output)
    if deterministic_result is not None:
        logger.debug("Deterministic classification: %s", deterministic_result)
        return deterministic_result

    # Layer 2: LLM classification for ambiguous cases
    messages = [
        {
            "role": "system",
            "content": (
                "Classify the coding agent output into exactly one category. "
                "Reply with ONE word only: "
                "working, done, stuck, idle, waiting_input, or committing.\n"
                "\n"
                "Categories:\n"
                "- working: actively writing code, running commands, exploring\n"
                "- done: task complete, ready to commit, successful finish message\n"
                "- stuck: error messages, exceptions, repeated failed attempts, frozen state\n"
                "- idle: no activity, paused without work, silent for extended period\n"
                "- waiting_input: explicitly waiting for user confirmation/input/feedback\n"
                "- committing: running git commands, preparing to push changes\n"
                "\n"
                "Few-shot examples:\n"
                "\n"
                "Example 1:\n"
                'Output: "Let me create the database schema first."\n'
                "Classification: working\n"
                "\n"
                "Example 2:\n"
                'Output: "I\'ve finished implementing the feature. All tests pass."\n'
                "Classification: done\n"
                "\n"
                "Example 3:\n"
                'Output: "Connection failed again after 3 attempts. Error: '
                'ConnectionRefusedError"\n'
                "Classification: stuck\n"
                "\n"
                "Example 4:\n"
                'Output: "No active work detected in last 60 seconds"\n'
                "Classification: idle\n"
                "\n"
                "Example 5:\n"
                'Output: "Waiting for your confirmation before proceeding with the refactoring."\n'
                "Classification: waiting_input\n"
                "\n"
                "Example 6:\n"
                "Output: \"git add src/ && git commit -m 'Add new feature'\"\n"
                "Classification: committing\n"
                "\n"
                "Respond with ONLY the category name, nothing else."
            ),
        },
        {"role": "user", "content": output[-2000:]},
    ]

    try:
        resp = chat_completion_local_first(messages, max_tokens=10, temperature=0)
        choices = resp.get("choices") or []
        if not choices:
            return "unknown"
        content = choices[0].get("message", {}).get("content") or ""
        choice = content.strip().lower()
        # Recognized classifications now include waiting_input and committing
        if choice in {"working", "done", "stuck", "idle", "waiting_input", "committing"}:
            return choice
        logger.debug("Unknown classification from LLM: %s", choice)
        return "unknown"
    except Exception as exc:
        logger.debug("Classification failed: %s", exc)
        return "unknown"


def poll_workers(
    project_root: str,
    session_root: str | None = None,
    *,
    panes: list[dict] | None = None,
    hooks: list[MonitorHook] | None = None,
) -> list[dict]:
    """Poll all active worker panes and classify their current state."""
    session_root = session_root or project_root
    workers = (
        panes
        if panes is not None
        else list_worker_panes(
            project_root, session_root, include_freshness=False, include_prompt=False
        )
    )

    active = [w for w in workers if w.get("state") == "active"]
    results = []

    for w in active:
        slug = w["slug"]
        alive = w.get("alive", False)
        output = tail_worker_log(session_root, slug, lines=50)

        if not output:
            classification = "idle"
        else:
            classification = classify_output(output, hooks)

        # list_worker_panes now includes base_sha; if not, we fallback to get_pane
        base_sha = w.get("base_sha", "")
        if not base_sha:
            raw = get_pane(session_root, slug)
            base_sha = raw.get("base_sha", "") if raw else ""

        branch = w.get("branch") or ""
        has_commits = _has_new_commits(project_root, branch, base_sha)

        # JSON serialize the hook if it matched
        hook_info = None
        if isinstance(classification, tuple):
            _, hook = classification
            hook_info = {
                "pattern": hook.pattern,
                "kind": hook.kind,
                "message": hook.message,
                "keystroke": hook.keystroke,
            }
            classification = "hook_match"
            # Persist to metadata so dgov status can see it (only if changed)
            last_match = w.get("metadata", {}).get("last_hook_match")
            if last_match != hook_info:
                set_pane_metadata(session_root, slug, last_hook_match=hook_info)

        results.append(
            {
                "slug": slug,
                "agent": w.get("agent"),
                "classification": classification,
                "has_commits": has_commits,
                "is_alive": alive,
                "output_preview": output[:100] if output else "",
                "hook_match": hook_info,
            }
        )

    return results


def _take_action(project_root: str, session_root: str, worker: dict, history: dict) -> str | None:
    """Evaluate history and take automated action if rules match.

    Only terminal states (done, stuck, idle) trigger remediation.
    Intermediate states (working, waiting_input, committing) are passive,
    but waiting_input can trigger a blocked event.
    """
    slug = worker["slug"]
    classification = worker["classification"]

    # Initialize history entry early so cooldown applies to all actions
    if slug not in history:
        history[slug] = {"classifications": [], "last_action_at": 0.0, "blocked_notified": False}

    hist = history[slug]

    # Handle hook-based actions first
    if classification == "hook_match" and worker.get("hook_match"):
        hook_data = worker["hook_match"]
        kind = hook_data["kind"]
        if kind == "nudge":
            _nudge_stuck(
                project_root,
                session_root,
                slug,
                message=hook_data.get("message"),
                keystroke=hook_data.get("keystroke"),
            )
            hist["last_action_at"] = time.time()
            return "hook_nudge"
        if kind == "fail":
            _mark_idle_failed(project_root, session_root, slug, reason="hook_fail")
            hist["last_action_at"] = time.time()
            return "hook_fail"
        if kind == "auto_complete":
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "hook_auto_complete"
        # If it's a state override, treat it as that state for default rules below
        if kind in {"done", "stuck", "idle", "working", "waiting_input", "committing"}:
            classification = kind

    hist["classifications"].append(classification)

    # Keep only last 10 for memory efficiency
    if len(hist["classifications"]) > 10:
        hist["classifications"] = hist["classifications"][-10:]

    # Count consecutive trailing same classifications
    consecutive = 0
    for c in reversed(hist["classifications"]):
        if c == classification:
            consecutive += 1
        else:
            break

    # Re-check state from DB to avoid TOCTOU race
    raw = get_pane(session_root, slug)
    if not raw or raw.get("state") != "active":
        return None

    # Handle stale workers (active but not alive)
    if not worker.get("is_alive", True):
        if worker.get("has_commits"):
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "stale_auto_complete"
        else:
            _mark_idle_failed(project_root, session_root, slug, reason="stale_process")
            hist["last_action_at"] = time.time()
            return "stale_fail"

    # Handle waiting_input (not terminal, but needs notification)
    if classification == "waiting_input":
        if consecutive >= 3 and not hist.get("blocked_notified"):
            emit_event(session_root, "monitor_blocked", slug, reason="waiting_input")
            hist["blocked_notified"] = True
            return "blocked_event"
        return None

    # Reset blocked notification if state changed
    if classification != "waiting_input":
        hist["blocked_notified"] = False

    # Skip remediation for non-terminal states
    if classification in {"working", "committing", "unknown"}:
        return None

    # Cooldown: skip if action was taken recently
    if time.time() - hist["last_action_at"] < 60:
        return None

    # Terminal state rules only
    if classification == "done":
        # If we have commits, one 'done' is enough. If not, wait for 2.
        if worker["has_commits"] or consecutive >= 2:
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "auto_complete"

    # Proactive cleanup: if worker has commits but is idling, auto-complete it
    if classification == "idle" and worker["has_commits"]:
        _auto_complete(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "proactive_auto_complete"

    if classification == "stuck" and consecutive >= 3:
        _nudge_stuck(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "nudge"

    if classification == "idle" and consecutive >= 4:
        _mark_idle_failed(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "idle_timeout"

    return None


def _auto_complete(project_root: str, session_root: str, slug: str) -> None:
    """Force a worker to 'done' state via signal file."""
    done_dir = Path(session_root, STATE_DIR, "done")
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / slug).touch()

    update_pane_state(session_root, slug, "done", force=True)
    emit_event(session_root, "monitor_auto_complete", slug, reason="monitor_auto_complete")
    logger.info("Monitor: auto-completed %s", slug)


def _nudge_stuck(
    project_root: str,
    session_root: str,
    slug: str,
    message: str | None = None,
    keystroke: str | None = None,
) -> None:
    """Send a nudge message to a stuck worker pane.

    Skips nudging headless workers (interactive agents forced to -p/--prompt
    mode) since they don't read stdin.
    """
    pane = get_pane(session_root, slug)
    if not pane:
        return

    pane_id = pane.get("pane_id")
    if not pane_id:
        return

    # Headless workers (interactive agents forced to non-interactive mode)
    # don't read stdin — nudging them is a no-op.
    agent_id = pane.get("agent", "")
    role = pane.get("role", "worker")
    if role == "worker" and agent_id:
        registry = load_registry(project_root)
        agent_def = registry.get(agent_id)
        if agent_def and agent_def.interactive:
            logger.info("Monitor: skipping nudge for headless worker %s (%s)", slug, agent_id)
            return

    backend = get_backend()
    if keystroke:
        backend.send_input(pane_id, keystroke)
        logger.info("Monitor: nudged worker %s with keystroke", slug)
    else:
        text = (
            message
            or "\n\nYou appear stuck. Commit changes and run: dgov worker complete -m 'summary'\n"
        )
        backend.send_input(pane_id, text)
        logger.info("Monitor: nudged stuck worker %s", slug)

    emit_event(session_root, "monitor_nudge", slug)


def _mark_idle_failed(
    project_root: str, session_root: str, slug: str, reason: str | None = None
) -> None:
    """Mark an idle worker as failed."""
    update_pane_state(session_root, slug, "failed", force=True)
    set_pane_metadata(session_root, slug, monitor_reason=reason or "idle_timeout")
    emit_event(session_root, "monitor_idle_timeout", slug, reason=reason or "monitor_idle_timeout")
    logger.info("Monitor: timed out idle worker %s (reason=%s)", slug, reason)


def run_monitor(
    project_root: str,
    session_root: str | None = None,
    *,
    poll_interval: int = 15,
    dry_run: bool = False,
) -> None:
    """Run the monitor loop."""
    session_root = session_root or project_root
    history: dict[str, dict] = {}

    # Ensure logging is configured for console output
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    monitor_dir = Path(session_root, STATE_DIR, "monitor")
    monitor_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting monitor on %s (interval %ds)", project_root, poll_interval)
    print(f"Monitor active: polling every {poll_interval}s...")

    tick = 0
    try:
        while True:
            try:
                # Reload hooks each tick for live updates
                hooks = load_monitor_hooks(session_root)

                # Prune stale panes every 10 ticks (~2.5 mins at 15s interval)
                if tick % 10 == 0:
                    pruned = prune_stale_panes(project_root, session_root)
                    if pruned:
                        logger.info("Monitor: pruned stale panes: %s", ", ".join(pruned))

                workers = poll_workers(project_root, session_root, hooks=hooks)
                actions = []

                for w in workers:
                    action = _take_action(project_root, session_root, w, history)
                    if action:
                        actions.append({"slug": w["slug"], "action": action})
                        print(f"[{time.strftime('%H:%M:%S')}] Action: {action} -> {w['slug']}")

                status = {
                    "timestamp": time.time(),
                    "workers": workers,
                    "actions": actions,
                }

                with open(monitor_dir / "status.json", "w") as f:
                    json.dump(status, f, indent=2)

                if workers:
                    worker_states = ", ".join(f"{w['slug']}={w['classification']}" for w in workers)
                    print(f"[{time.strftime('%H:%M:%S')}] Tick {tick}: {worker_states}")
                elif tick % 4 == 0:
                    # Heartbeat print when idle
                    print(f"[{time.strftime('%H:%M:%S')}] Tick {tick}: idle")

            except Exception:
                logger.warning("Monitor tick failed", exc_info=True)

            if dry_run:
                return

            time.sleep(poll_interval)
            tick += 1
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
        print("\nMonitor stopped.")
