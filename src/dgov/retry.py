"""Auto-retry engine for failed/abandoned worker panes."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from dgov.agents import load_registry
from dgov.persistence import _STATE_DIR, _emit_event, _get_pane

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 0
    escalate_to: str | None = None
    backoff_base: float = 5.0


def _load_events(session_root: str) -> list[dict]:
    """Read all events from the journal."""
    events_path = Path(session_root) / _STATE_DIR / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _slug_lineage(session_root: str, slug: str) -> list[str]:
    """Walk retried_from links back to the original slug, return all slugs in chain."""
    chain = [slug]
    visited: set[str] = {slug}
    current = slug
    while True:
        rec = _get_pane(session_root, current)
        if not rec:
            break
        parent = rec.get("retried_from")
        if not parent or parent in visited:
            break
        chain.append(parent)
        visited.add(parent)
        current = parent
    return chain


def _count_retries(session_root: str, slug: str) -> int:
    """Count pane_auto_retried events for the slug lineage."""
    lineage = set(_slug_lineage(session_root, slug))
    events = _load_events(session_root)
    count = 0
    for ev in events:
        if ev.get("event") == "pane_auto_retried" and ev.get("pane") in lineage:
            count += 1
    return count


def retry_context(slug: str, session_root: str) -> str:
    """Build a failure context string from the pane's last output and events."""
    rec = _get_pane(session_root, slug)
    if not rec:
        return ""

    parts: list[str] = []

    # Try to read the pane log
    log_path = Path(session_root) / _STATE_DIR / "logs" / f"{slug}.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-20:] if len(lines) > 20 else lines
            if tail:
                parts.append("Last output:\n" + "\n".join(tail))
        except OSError:
            pass

    # Check for exit code
    exit_path = Path(session_root) / _STATE_DIR / "done" / f"{slug}.exit"
    if exit_path.exists():
        try:
            code = exit_path.read_text().strip()
            parts.append(f"Exit code: {code}")
        except OSError:
            pass

    # Recent events for this slug
    events = _load_events(session_root)
    slug_events = [ev for ev in events if ev.get("pane") == slug]
    recent = slug_events[-5:] if len(slug_events) > 5 else slug_events
    if recent:
        summaries = [f"  {ev.get('event', '?')}: {ev.get('ts', '?')}" for ev in recent]
        parts.append("Recent events:\n" + "\n".join(summaries))

    return "\n\n".join(parts)


def get_retry_policy(session_root: str, slug: str) -> RetryPolicy | None:
    """Look up the retry policy for a pane's agent.

    Per-pane ``max_retries`` override (set via CLI ``--max-retries``) takes
    priority over the agent's default. Returns None if no retries configured.
    """
    rec = _get_pane(session_root, slug)
    if not rec:
        return None

    # Per-pane override
    pane_max = rec.get("max_retries")

    agent_id = rec.get("agent", "")
    project_root = rec.get("project_root", "")
    registry = load_registry(project_root or None)
    agent_def = registry.get(agent_id)

    if pane_max is not None:
        max_retries = int(pane_max)
    elif agent_def:
        max_retries = agent_def.max_retries
    else:
        max_retries = 0

    if max_retries <= 0:
        return None

    escalate_to = agent_def.retry_escalate_to if agent_def else None
    return RetryPolicy(
        max_retries=max_retries,
        escalate_to=escalate_to,
    )


def maybe_auto_retry(
    session_root: str,
    slug: str,
    project_root: str,
) -> dict | None:
    """Auto-retry a failed/abandoned pane if its agent has a retry policy.

    Returns:
        {"retried": slug, "new_slug": ..., "attempt": N} on retry
        {"escalated": slug, "to": agent} on escalation
        None if retries exhausted or no policy
    """
    import dgov.panes as _p

    rec = _get_pane(session_root, slug)
    if not rec:
        return None

    state = rec.get("state", "")
    if state not in ("failed", "abandoned"):
        return None

    policy = get_retry_policy(session_root, slug)
    if not policy:
        return None

    attempt = _count_retries(session_root, slug)

    if attempt < policy.max_retries:
        # Backoff
        delay = policy.backoff_base * (attempt + 1)
        time.sleep(delay)

        # Build enhanced prompt
        original_prompt = rec.get("prompt", "")
        context = retry_context(slug, session_root)
        enhanced = original_prompt
        if context:
            enhanced += (
                f"\n\nPrevious attempt failed. Error context:\n{context}\nAvoid the same failure."
            )
        else:
            enhanced += "\n\nPrevious attempt failed. Avoid the same failure."

        _emit_event(
            session_root,
            "pane_auto_retried",
            slug,
            attempt=attempt + 1,
            max_retries=policy.max_retries,
        )

        result = _p.retry_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            prompt=enhanced,
        )

        if result.get("error"):
            logger.warning("Auto-retry failed for %s: %s", slug, result["error"])
            return None

        return {
            "retried": slug,
            "new_slug": result.get("new_slug", ""),
            "attempt": attempt + 1,
        }

    # Exhausted retries — try escalation
    if policy.escalate_to:
        _emit_event(
            session_root,
            "pane_auto_retried",
            slug,
            attempt=attempt + 1,
            escalated_to=policy.escalate_to,
        )

        result = _p.escalate_worker_pane(
            project_root,
            slug,
            target_agent=policy.escalate_to,
            session_root=session_root,
        )

        if result.get("error"):
            logger.warning("Auto-escalation failed for %s: %s", slug, result["error"])
            return None

        return {
            "escalated": slug,
            "to": policy.escalate_to,
            "new_slug": result.get("new_slug", ""),
        }

    return None
