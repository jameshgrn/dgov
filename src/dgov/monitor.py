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


def classify_output(output: str) -> str:
    """Classify agent output into working, done, stuck, or idle using local LLM."""
    if not output.strip():
        return "idle"

    messages = [
        {
            "role": "system",
            "content": (
                "Classify the coding agent output. "
                "Reply with one word: working, done, stuck, or idle."
            ),
        },
        {"role": "user", "content": output[-2000:]},
    ]

    try:
        resp = chat_completion_local_first(messages, max_tokens=10, temperature=0)
        choice = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip().lower()
        if choice in {"working", "done", "stuck", "idle"}:
            return choice
        return "unknown"
    except Exception as exc:
        logger.debug("Classification failed: %s", exc)
        return "unknown"


def poll_workers(project_root: str, session_root: str | None = None) -> list[dict]:
    """Poll all active worker panes and classify their current state."""
    session_root = session_root or project_root
    workers = list_worker_panes(
        project_root, session_root, include_freshness=False, include_prompt=False
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
    """Evaluate history and take automated action if rules match."""
    slug = worker["slug"]
    classification = worker["classification"]

    if slug not in history:
        history[slug] = {"classifications": [], "last_action_at": 0.0}

    hist = history[slug]
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

    # Rules
    if classification == "done" and worker["has_commits"] and consecutive >= 2:
        _auto_complete(project_root, session_root, slug)
        return "auto_complete"

    if classification == "stuck" and consecutive >= 3:
        _nudge_stuck(project_root, session_root, slug)
        return "nudge"

    if classification == "idle" and consecutive >= 4:
        _mark_idle_failed(project_root, session_root, slug)
        return "idle_timeout"

    return None


def _auto_complete(project_root: str, session_root: str, slug: str) -> None:
    """Force a worker to 'done' state via signal file."""
    done_dir = Path(session_root, STATE_DIR, "done")
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / slug).touch()

    update_pane_state(session_root, slug, "done", force=True)
    emit_event(session_root, "pane_done", slug, reason="monitor_auto_complete")
    logger.info("Monitor: auto-completed %s", slug)


def _nudge_stuck(project_root: str, session_root: str, slug: str) -> None:
    """Send a nudge message to a stuck worker pane."""
    pane = get_pane(session_root, slug)
    if not pane:
        return

    pane_id = pane.get("pane_id")
    if not pane_id:
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
    emit_event(session_root, "pane_done", slug, reason="monitor_idle_timeout")
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
