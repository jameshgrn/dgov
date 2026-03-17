"""Lightweight polling daemon for worker state classification and auto-remediation.

Uses local Qwen 4B (localhost:8082) to classify worker outputs and
take automated actions (auto-complete, nudge, idle timeout).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from dgov.backend import get_backend
from dgov.done import _has_new_commits
from dgov.openrouter import chat_completion_local_first
from dgov.persistence import (
    STATE_DIR,
    emit_event,
    get_pane,
    set_pane_metadata,
    update_pane_state,
)
from dgov.status import list_worker_panes, tail_worker_log

logger = logging.getLogger(__name__)


# Deterministic regex patterns for classification
DETERMINISTIC_PATTERNS = {
    "idle": [
        r"\b(no[ \t]+work|[aA]waiting\s+input|pause[d]?|waiting\b|idling)\b",
    ],
    "done": [
        r"\b(done|complete[d]?|finish[e]?d?|\bready\b|success|all\.done)",
    ],
    "failed": [
        r"\b(failed|error|exception|traceback|crash|panic|fatal)\b",
    ],
    "waiting_input": [
        r"\b(waiting[ \t]+for\s+(user|input|confirmation|approval|prompt))\b",
        r"\b(paused.*awaiting\b|awaiting.*input\b)",
        r"^\s*#\s*(TODO|FIXME|XXX|HACK):",
    ],
    "committing": [
        r"\b(commit|git\s+add|git\s+commit|pushing|pushed|committed)\b",
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
    """Match a regex pattern against text, handling case-insensitivity."""
    import re

    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        logger.debug("Invalid regex pattern: %s", pattern)
        return False


def classify_output(output: str) -> str:
    """Classify agent output into working, done, stuck, idle, waiting_input, or committing.

    Uses deterministic regex patterns first for efficiency, falls back to LLM for
    ambiguous cases. This two-layer approach minimizes API calls while maintaining accuracy.
    """
    if not output.strip():
        return "idle"

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
    project_root: str, session_root: str | None = None, *, panes: list[dict] | None = None
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

    active = [w for w in workers if w.get("state") == "active" and w.get("alive")]
    results = []

    for w in active:
        slug = w["slug"]
        output = tail_worker_log(session_root, slug, lines=20)

        if not output:
            classification = "idle"
        else:
            classification = classify_output(output)

        # list_worker_panes doesn't include base_sha; fetch from raw pane record
        raw = get_pane(session_root, slug)
        base_sha = raw.get("base_sha", "") if raw else ""
        branch = w.get("branch") or ""
        has_commits = _has_new_commits(project_root, branch, base_sha)

        results.append(
            {
                "slug": slug,
                "agent": w.get("agent"),
                "classification": classification,
                "has_commits": has_commits,
                "output_preview": output[:100] if output else "",
            }
        )

    return results


def _take_action(project_root: str, session_root: str, worker: dict, history: dict) -> str | None:
    """Evaluate history and take automated action if rules match.

    Only terminal states (done, stuck, idle) trigger remediation.
    Intermediate states (working, waiting_input, committing) are passive.
    """
    slug = worker["slug"]
    classification = worker["classification"]

    # Skip remediation for non-terminal states
    if classification in {"working", "waiting_input", "committing", "unknown"}:
        return None

    if slug not in history:
        history[slug] = {"classifications": [], "last_action_at": 0.0}

    hist = history[slug]
    hist["classifications"].append(classification)

    # Keep only last 10 for memory efficiency
    if len(hist["classifications"]) > 10:
        hist["classifications"] = hist["classifications"][-10:]

    # Cooldown: skip if action was taken recently
    if time.time() - hist["last_action_at"] < 60:
        return None

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

    # Terminal state rules only
    if classification == "done" and worker["has_commits"] and consecutive >= 2:
        _auto_complete(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "auto_complete"

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


def _nudge_stuck(project_root: str, session_root: str, slug: str) -> None:
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
        from dgov.agents import load_registry

        registry = load_registry(project_root)
        agent_def = registry.get(agent_id)
        if agent_def and agent_def.interactive:
            logger.info("Monitor: skipping nudge for headless worker %s (%s)", slug, agent_id)
            return

    get_backend().send_input(
        pane_id,
        "\n\nYou appear stuck. Commit changes and run: dgov worker complete -m 'summary'\n",
    )
    emit_event(session_root, "monitor_nudge", slug)
    logger.info("Monitor: nudged stuck worker %s", slug)


def _mark_idle_failed(project_root: str, session_root: str, slug: str) -> None:
    """Mark an idle worker as failed."""
    update_pane_state(session_root, slug, "failed", force=True)
    set_pane_metadata(session_root, slug, monitor_reason="idle_timeout")
    emit_event(session_root, "monitor_idle_timeout", slug, reason="monitor_idle_timeout")
    logger.info("Monitor: timed out idle worker %s", slug)


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

    monitor_dir = Path(session_root, STATE_DIR, "monitor")
    monitor_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting monitor on %s (interval %ds)", project_root, poll_interval)

    try:
        while True:
            try:
                workers = poll_workers(project_root, session_root)
                actions = []

                for w in workers:
                    action = _take_action(project_root, session_root, w, history)
                    if action:
                        actions.append({"slug": w["slug"], "action": action})

                status = {
                    "timestamp": time.time(),
                    "workers": workers,
                    "actions": actions,
                }

                with open(monitor_dir / "status.json", "w") as f:
                    json.dump(status, f, indent=2)
            except Exception:
                logger.warning("Monitor tick failed", exc_info=True)

            if dry_run:
                return

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
