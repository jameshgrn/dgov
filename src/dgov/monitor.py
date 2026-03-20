"""Lightweight polling daemon for worker state classification and auto-remediation.

Uses local Qwen 4B (localhost:8082) to classify worker outputs and
take automated actions (auto-complete, nudge, idle timeout).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.agents import load_registry
from dgov.backend import get_backend
from dgov.decision import MonitorOutputRequest, ProviderError
from dgov.done import _has_new_commits
from dgov.executor import EscalateResult, RetryResult
from dgov.monitor_hooks import load_monitor_hooks, match_monitor_hook
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    emit_event,
    get_pane,
    latest_event_id,
    read_events,
    set_pane_metadata,
    take_dispatch_queue,
    wait_for_events,
)
from dgov.status import list_worker_panes, prune_stale_panes, tail_worker_log

if TYPE_CHECKING:
    from dgov.monitor_hooks import MonitorHook

logger = logging.getLogger(__name__)

_MONITOR_WAKE_EVENTS = (
    "dispatch_queued",
    "pane_created",
    "pane_done",
    "pane_failed",
    "pane_timed_out",
    "pane_merged",
    "pane_merge_failed",
    "pane_escalated",
    "pane_superseded",
    "pane_closed",
    "pane_retry_spawned",
    "pane_auto_retried",
    "pane_review_pending",
    "mission_pending",
    "mission_running",
    "mission_waiting",
    "mission_reviewing",
    "mission_merging",
    "mission_completed",
    "mission_failed",
    "dag_started",
    "dag_task_dispatched",
    "dag_task_completed",
    "dag_task_failed",
    "dag_task_escalated",
    "dag_completed",
    "dag_failed",
    "merge_completed",
    "monitor_auto_complete",
    "monitor_idle_timeout",
    "monitor_blocked",
    "monitor_auto_merge",
    "monitor_auto_retry",
)

_ACTIVE_ADD_EVENTS = frozenset({"pane_created", "pane_resumed"})
_ACTIVE_REMOVE_EVENTS = frozenset(
    {
        "pane_done",
        "pane_failed",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)
_MERGE_CLEAR_EVENTS = frozenset(
    {
        "pane_failed",
        "pane_review_pending",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)
_RETRY_CLEAR_EVENTS = frozenset(
    {
        "pane_done",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)


@dataclass
class MonitorLoopState:
    event_cursor: int
    active_slugs: set[str] = field(default_factory=set)
    merge_candidates: set[str] = field(default_factory=set)
    retry_candidates: set[str] = field(default_factory=set)
    queue_dirty: bool = False


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
    output: str,
    hooks: list[MonitorHook] | None = None,
    *,
    session_root: str | None = None,
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
    try:
        from dgov.decision import DecisionKind
        from dgov.provider_registry import get_provider

        provider = get_provider(DecisionKind.CLASSIFY_OUTPUT, session_root=session_root)
        result = provider.classify_output(MonitorOutputRequest(output=output))
        return result.decision.classification
    except ProviderError as exc:
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
            result = classify_output(output, hooks, session_root=session_root)
            classification = result if isinstance(result, str) else result[0]

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


def _bootstrap_monitor_state(
    session_root: str, *, auto_merge: bool, auto_retry: bool
) -> MonitorLoopState:
    """Seed monitor state from persisted pane records once at startup."""
    panes = all_panes(session_root)
    return MonitorLoopState(
        event_cursor=latest_event_id(session_root),
        active_slugs={pane["slug"] for pane in panes if pane.get("state") == "active"},
        merge_candidates={
            pane["slug"] for pane in panes if auto_merge and pane.get("state") == "done"
        },
        retry_candidates={
            pane["slug"]
            for pane in panes
            if auto_retry and pane.get("state") in {"failed", "abandoned"}
        },
        queue_dirty=(Path(session_root) / ".dgov" / "dispatch_queue.jsonl").is_file(),
    )


def _apply_monitor_events(
    state: MonitorLoopState,
    events: list[dict],
    *,
    auto_merge: bool,
    auto_retry: bool,
) -> None:
    """Update monitor candidate sets from journal events."""
    for event in events:
        state.event_cursor = max(state.event_cursor, int(event.get("id", state.event_cursor)))
        kind = str(event.get("event", ""))
        slug = str(event.get("pane", ""))

        if kind == "dispatch_queued":
            state.queue_dirty = True
            continue

        if not slug or slug in {"monitor", "dispatch-queue"}:
            continue

        if kind in _ACTIVE_ADD_EVENTS:
            state.active_slugs.add(slug)
        elif kind in _ACTIVE_REMOVE_EVENTS:
            state.active_slugs.discard(slug)

        if kind == "pane_done":
            state.retry_candidates.discard(slug)
            if auto_merge:
                state.merge_candidates.add(slug)
        elif kind in _MERGE_CLEAR_EVENTS:
            state.merge_candidates.discard(slug)

        if kind == "pane_failed":
            state.merge_candidates.discard(slug)
            if auto_retry:
                state.retry_candidates.add(slug)
        elif kind == "monitor_idle_timeout":
            if auto_retry:
                state.retry_candidates.add(slug)
        elif kind in _RETRY_CLEAR_EVENTS:
            state.retry_candidates.discard(slug)


def _tracked_worker_records(
    project_root: str, session_root: str, active_slugs: set[str]
) -> list[dict]:
    """Fetch current pane records for the active slugs the monitor owns."""
    if not active_slugs:
        return []
    workers = list_worker_panes(
        project_root, session_root, include_freshness=False, include_prompt=False
    )
    return [
        worker
        for worker in workers
        if worker.get("slug") in active_slugs and worker.get("state") == "active"
    ]


def _drain_dispatch_queue(project_root: str, session_root: str) -> list[dict]:
    """Dispatch all currently queued prompts once the queue is marked dirty."""
    queued = take_dispatch_queue(session_root)
    actions: list[dict] = []
    for item in queued:
        summary = item.get("summary", "queued task")
        agent = item.get("agent_hint") or "qwen-35b"
        try:
            from dgov.lifecycle import create_worker_pane

            pane = create_worker_pane(
                project_root=project_root,
                prompt=summary,
                agent=agent,
                permission_mode="bypassPermissions",
                session_root=session_root,
            )
            logger.info("Monitor: drained queue -> %s (%s)", pane.slug, agent)
            actions.append({"slug": pane.slug, "action": "queue_dispatch"})
        except Exception:
            logger.warning(
                "Monitor: queue dispatch failed for: %s",
                summary,
                exc_info=True,
            )
    return actions


def _process_auto_merge_candidates(
    project_root: str,
    session_root: str,
    state: MonitorLoopState,
    merge_attempted: set[str],
) -> list[dict]:
    """Attempt auto-merge for slugs marked done by the event journal."""
    return _process_candidate_set(
        project_root,
        session_root,
        candidates=state.merge_candidates,
        attempted=merge_attempted,
        valid_states={"done"},
        action_fn=_try_auto_merge,
        on_success=lambda slug: state.active_slugs.discard(slug),
    )


def _resolve_retry_successor_slug(session_root: str, slug: str) -> str | None:
    """Resolve the new pane slug created by retry/escalation side effects."""
    pane_after = get_pane(session_root, slug) or {}
    new_slug = str(pane_after.get("superseded_by", ""))
    if new_slug:
        return new_slug
    for event in reversed(read_events(session_root, slug=slug, limit=5)):
        candidate = str(event.get("new_slug", ""))
        if candidate:
            return candidate
    return None


def _track_retry_successor(state: MonitorLoopState, session_root: str, slug: str) -> None:
    """Track the new active pane created by an auto-retry or escalation."""
    new_slug = _resolve_retry_successor_slug(session_root, slug)
    if new_slug:
        state.active_slugs.add(new_slug)


def _process_auto_retry_candidates(
    project_root: str,
    session_root: str,
    state: MonitorLoopState,
    retry_attempted: set[str],
) -> list[dict]:
    """Attempt auto-retry for failed panes tracked from journal events."""
    return _process_candidate_set(
        project_root,
        session_root,
        candidates=state.retry_candidates,
        attempted=retry_attempted,
        valid_states={"failed", "abandoned"},
        action_fn=_try_auto_retry,
        on_success=lambda slug: _track_retry_successor(state, session_root, slug),
    )


def _process_candidate_set(
    project_root: str,
    session_root: str,
    *,
    candidates: set[str],
    attempted: set[str],
    valid_states: set[str],
    action_fn,
    on_success,
) -> list[dict]:
    """Process a monitor candidate set through a single policy loop."""
    actions: list[dict] = []
    for slug in sorted(candidates):
        pane = get_pane(session_root, slug)
        if not pane or pane.get("state") not in valid_states or slug in attempted:
            candidates.discard(slug)
            continue
        try:
            act = action_fn(project_root, session_root, slug)
            if act:
                actions.append({"slug": slug, "action": act})
                print(f"[{time.strftime('%H:%M:%S')}] {act}: {slug}")
                candidates.discard(slug)
                on_success(slug)
            else:
                attempted.add(slug)
                candidates.discard(slug)
        except Exception:
            logger.warning("Auto-action error for %s", slug, exc_info=True)
            attempted.add(slug)
            candidates.discard(slug)
    return actions


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
    """Force a worker to 'done' state via signal file.

    Defense in depth: only complete if there are actual commits beyond base
    when branch/base info is available. If no commits and info is available,
    do nothing (worker may still be working).
    """
    # Get pane record to check branch/base info
    pane = get_pane(session_root, slug)
    if not pane:
        return

    branch_name = pane.get("branch_name", "")
    base_sha = pane.get("base_sha", "")
    project_root_from_pane = pane.get("project_root", "")

    # Require commits when branch/base info is available
    if branch_name and base_sha and project_root_from_pane:
        has_commits = _has_new_commits(project_root_from_pane, branch_name, base_sha)
        if not has_commits:
            # No commits yet — don't auto-complete, worker may still be working
            logger.debug(
                "Monitor: skipping auto-complete for %s — no commits beyond %s",
                slug,
                base_sha[:8],
            )
            return

    done_dir = Path(session_root, STATE_DIR, "done")
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / slug).touch()

    from dgov.executor import run_complete_pane

    result = run_complete_pane(
        project_root, slug, session_root=session_root, reason="auto_complete"
    )
    if not result.changed:
        logger.debug("Monitor: pane %s already in done state", slug)
        return
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
    from dgov.executor import run_fail_pane

    result = run_fail_pane(
        project_root, slug, session_root=session_root, reason=reason or "idle_timeout"
    )
    if not result.changed:
        logger.debug("Monitor: pane %s already in failed state", slug)
        return
    set_pane_metadata(session_root, slug, monitor_reason=reason or "idle_timeout")
    logger.info("Monitor: timed out idle worker %s (reason=%s)", slug, reason)


def _try_auto_merge(project_root: str, session_root: str, slug: str) -> str | None:
    """Attempt to auto-merge a done pane if review verdict is safe."""
    from dgov.executor import run_land_only

    result = run_land_only(project_root, slug, session_root=session_root)
    if result.error:
        log = logger.warning if result.failure_stage == "review_error" else logger.info
        log("Skip auto-merge %s: %s", slug, result.error)
        return None
    if result.merge_result and result.merge_result.get("merged"):
        emit_event(session_root, "monitor_auto_merge", slug)
        logger.info("Monitor: auto-merged %s", slug)
        return "auto_merge"
    logger.warning("Auto-merge failed for %s: %s", slug, result.error)
    return None


def _try_auto_retry(project_root: str, session_root: str, slug: str) -> str | None:
    """Attempt to auto-retry a failed pane using its agent retry policy."""
    from dgov.executor import run_retry_or_escalate

    result = run_retry_or_escalate(project_root, slug, session_root=session_root)
    if not hasattr(result, "new_slug") or not result.new_slug:
        return None
    if isinstance(result, RetryResult):
        emit_event(
            session_root,
            "monitor_auto_retry",
            slug,
            new_slug=result.new_slug,
        )
        logger.info("Monitor: auto-retried %s -> %s", slug, result.new_slug)
        return "auto_retry"
    if isinstance(result, EscalateResult):
        emit_event(
            session_root,
            "monitor_auto_retry",
            slug,
            escalated_to=result.target_agent,
            new_slug=result.new_slug or "",
        )
        logger.info("Monitor: auto-escalated %s -> %s", slug, result.target_agent)
        return "auto_escalate"
    return None


def _wait_for_monitor_wakeup(session_root: str, after_id: int, timeout_s: int) -> list[dict]:
    """Wait for journal activity that should wake the monitor early."""
    return wait_for_events(
        session_root,
        after_id=after_id,
        event_types=_MONITOR_WAKE_EVENTS,
        timeout_s=float(timeout_s),
    )


def run_monitor(
    project_root: str,
    session_root: str | None = None,
    *,
    poll_interval: int = 5,
    dry_run: bool = False,
    auto_merge: bool = False,
    auto_retry: bool = True,
) -> None:
    """Run the monitor loop."""
    session_root = session_root or project_root
    state = _bootstrap_monitor_state(session_root, auto_merge=auto_merge, auto_retry=auto_retry)
    history: dict[str, dict] = {}
    merge_attempted: set[str] = set()
    retry_attempted: set[str] = set()
    pending_events: list[dict] = []

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
                _apply_monitor_events(
                    state,
                    pending_events,
                    auto_merge=auto_merge,
                    auto_retry=auto_retry,
                )

                # Reload hooks each tick for live updates
                hooks = load_monitor_hooks(session_root)

                # Prune stale panes every 30 ticks (~2.5 mins at 5s interval)
                if tick % 30 == 0:
                    pruned = prune_stale_panes(project_root, session_root)
                    if pruned:
                        logger.info("Monitor: pruned stale panes: %s", ", ".join(pruned))

                actions = []

                if state.queue_dirty:
                    queue_actions = _drain_dispatch_queue(project_root, session_root)
                    actions.extend(queue_actions)
                    for queue_action in queue_actions:
                        state.active_slugs.add(queue_action["slug"])
                    state.queue_dirty = False

                tracked_workers = _tracked_worker_records(
                    project_root, session_root, state.active_slugs
                )
                workers = poll_workers(
                    project_root,
                    session_root,
                    panes=tracked_workers,
                    hooks=hooks,
                )

                for w in workers:
                    action = _take_action(project_root, session_root, w, history)
                    if action:
                        actions.append({"slug": w["slug"], "action": action})
                        print(f"[{time.strftime('%H:%M:%S')}] Action: {action} -> {w['slug']}")
                        if action in {
                            "auto_complete",
                            "stale_auto_complete",
                            "proactive_auto_complete",
                            "hook_auto_complete",
                        }:
                            state.active_slugs.discard(w["slug"])
                            if auto_merge:
                                state.merge_candidates.add(w["slug"])
                        elif action in {"stale_fail", "idle_timeout", "hook_fail"}:
                            state.active_slugs.discard(w["slug"])
                            if auto_retry:
                                state.retry_candidates.add(w["slug"])

                if auto_merge:
                    actions.extend(
                        _process_auto_merge_candidates(
                            project_root,
                            session_root,
                            state,
                            merge_attempted,
                        )
                    )
                if auto_retry:
                    actions.extend(
                        _process_auto_retry_candidates(
                            project_root,
                            session_root,
                            state,
                            retry_attempted,
                        )
                    )

                status = {
                    "timestamp": time.time(),
                    "workers": workers,
                    "actions": actions,
                }

                with open(monitor_dir / "status.json", "w") as f:
                    json.dump(status, f, indent=2)

                if workers:
                    worker_states = ", ".join(
                        f"{w['slug']}={w['classification']}" for w in workers
                    )
                    logger.info("Tick %d: %s", tick, worker_states)
                    emit_event(session_root, "monitor_tick", "monitor", states=worker_states)
                elif tick % 4 == 0:
                    # Heartbeat print when idle
                    logger.info("Tick %d: idle", tick)
                    emit_event(session_root, "monitor_tick", "monitor", states="idle")

            except Exception:
                logger.warning("Monitor tick failed", exc_info=True)

            if dry_run:
                return

            state.event_cursor = latest_event_id(session_root)
            pending_events = _wait_for_monitor_wakeup(
                session_root,
                state.event_cursor,
                poll_interval,
            )
            tick += 1
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
        print("\nMonitor stopped.")
