"""Pane recovery: retry policy, escalation, and bounded retry with auto-escalation."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from dgov.agents import load_registry
from dgov.lifecycle import close_worker_pane, create_worker_pane
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    emit_event,
    get_pane,
    read_events,
    set_pane_metadata,
    update_pane_state,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 0
    escalate_to: str | None = None
    backoff_base: float = 5.0


# Default escalation chain: maps agents to the next-tier logical name.
# Logical names get resolved by the router to available physical backends.
# Escalation proceeds: 4b → 9b → 35b → 122b → 397b (ceiling).
# qwen-max is governor/frontier only — not a worker escalation target.
ESCALATION_CHAIN: dict[str, str] = {
    # Logical names (preferred — resolved by router)
    "qwen-4b": "qwen-9b",
    "qwen-9b": "qwen-35b",
    "qwen-35b": "qwen-122b",
    "qwen-122b": "qwen-397b",
    "qwen-397b": "qwen-397b",  # ceiling — no further worker escalation
    # Physical names (backward compat for panes that stored physical agent)
    "river-4b": "qwen-9b",
    "river-9b": "qwen-35b",
    "river-9b-2": "qwen-35b",
    "river-9b-3": "qwen-35b",
    "river-35b": "qwen-122b",
    "river-35b-2": "qwen-122b",
    "qwen35-9b": "qwen-35b",
    "qwen35-flash": "qwen-35b",
    "qwen35-35b": "qwen-122b",
    "qwen35-122b": "qwen-397b",
    "qwen35-397b": "qwen-397b",  # ceiling
}


def escalate_worker_pane(
    project_root: str,
    slug: str,
    target_agent: str = "qwen-35b",
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> dict:
    """Escalate a worker pane to a different agent.

    Closes the existing pane and relaunches with ``target_agent``
    using the same prompt. Returns the new pane info.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    original_prompt = target.get("prompt", "")
    if not original_prompt:
        return {"error": f"No prompt recorded for {slug}"}

    original_agent = target.get("agent", "unknown")

    # Build failure context from the old pane (log tail, exit code, events)
    context = retry_context(slug, session_root)
    if context:
        original_prompt = (
            original_prompt
            + "\n\n--- Prior attempt failed (agent: "
            + original_agent
            + ") ---\n"
            + context
        )

    # Create the new pane first, then close the old one
    # Compute escalation slug with collision avoidance
    base_slug = re.sub(r"-esc(-\d+)?$", "", slug)  # strip existing -esc or -esc-N
    esc_num = 1
    existing = all_panes(session_root)
    for p in existing:
        m = re.match(rf"^{re.escape(base_slug)}-esc-(\d+)$", p.get("slug", ""))
        if m:
            esc_num = max(esc_num, int(m.group(1)) + 1)
    new_slug = f"{base_slug}-esc-{esc_num}"
    try:
        new_pane = create_worker_pane(
            project_root=project_root,
            prompt=original_prompt,
            agent=target_agent,
            permission_mode=permission_mode,
            slug=new_slug,
            session_root=session_root,
        )
    except Exception as e:
        return {"error": str(e)}

    # Mark old pane as escalated then close
    update_pane_state(session_root, slug, "escalated")
    emit_event(session_root, "pane_escalated", slug, new_slug=new_slug, target_agent=target_agent)
    close_worker_pane(project_root, slug, session_root=session_root)

    return {
        "escalated": True,
        "original_slug": slug,
        "original_agent": original_agent,
        "new_slug": new_pane.slug,
        "agent": target_agent,
        "pane_id": new_pane.pane_id,
        "worktree": new_pane.worktree_path,
    }


def retry_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    agent: str | None = None,
    prompt: str | None = None,
    permission_mode: str = "bypassPermissions",
    close: bool = False,
) -> dict:
    """Retry a pane by creating a new one linked to the original.

    Reads original pane record (prompt, agent, base_sha), computes a new
    slug ``<original-base>-<attempt+1>``, creates a new worktree + branch +
    pane via the normal create path, then cross-links the old and new records.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    original_prompt = prompt or target.get("prompt", "")
    original_agent = agent or target.get("agent", "claude")

    if close:
        from dgov.lifecycle import close_worker_pane

        close_worker_pane(project_root, slug, session_root=session_root)

    # Compute attempt number from slug pattern
    base_slug = re.sub(r"-\d+$", "", slug)  # strip trailing -N
    attempt = 1
    existing = all_panes(session_root)
    for p in existing:
        m = re.match(rf"^{re.escape(base_slug)}-(\d+)$", p.get("slug", ""))
        if m:
            attempt = max(attempt, int(m.group(1)))
    attempt += 1
    new_slug = f"{base_slug}-{attempt}"

    # Create new pane
    try:
        new_pane = create_worker_pane(
            project_root=project_root,
            prompt=original_prompt,
            agent=original_agent,
            permission_mode=permission_mode,
            slug=new_slug,
            session_root=session_root,
        )
    except Exception as e:
        return {"error": str(e)}

    # Link records via SQLite metadata
    set_pane_metadata(session_root, new_slug, retried_from=slug)
    set_pane_metadata(session_root, slug, superseded_by=new_slug)
    update_pane_state(session_root, slug, "superseded", force=True)

    # Emit events
    emit_event(session_root, "pane_retry_spawned", slug, new_slug=new_slug, attempt=attempt)
    emit_event(session_root, "pane_retry_spawned", new_slug, retried_from=slug, attempt=attempt)
    emit_event(session_root, "pane_superseded", slug, superseded_by=new_slug)

    return {
        "retried": True,
        "original_slug": slug,
        "new_slug": new_pane.slug,
        "agent": original_agent,
        "attempt": attempt,
        "pane_id": new_pane.pane_id,
    }


def retry_or_escalate(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    max_retries: int = 2,
    permission_mode: str = "bypassPermissions",
) -> dict:
    """Retry a failed pane, auto-escalating after *max_retries* at the same tier.

    Tracks ``retry_count`` in pane metadata. When the count reaches
    *max_retries*, the pane is escalated to the next agent in
    ``ESCALATION_CHAIN`` (or the agent's own ``retry_escalate_to``
    if configured) and the counter resets.

    Returns ``{'action': 'retry'|'escalate', 'agent': ..., 'retry_count': N, ...}``.
    """
    session_root = os.path.abspath(session_root or project_root)
    rec = get_pane(session_root, slug)
    if not rec:
        return {"error": f"Pane not found: {slug}"}

    current_agent = rec.get("agent", "unknown")
    retry_count = int(rec.get("retry_count", 0))

    # Per-pane max_retries override takes priority
    pane_max = rec.get("max_retries")
    if pane_max is not None:
        max_retries = int(pane_max)

    if retry_count < max_retries:
        # Retry with the same agent
        result = retry_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            permission_mode=permission_mode,
        )
        if result.get("error"):
            return result

        new_count = retry_count + 1
        set_pane_metadata(session_root, result["new_slug"], retry_count=new_count)

        return {
            "action": "retry",
            "agent": current_agent,
            "retry_count": new_count,
            "original_slug": slug,
            "new_slug": result["new_slug"],
        }

    # Exhausted retries — escalate to next agent
    # Check agent-level escalate_to first, then fall back to ESCALATION_CHAIN
    next_agent = _resolve_escalation_target(current_agent, project_root)

    if next_agent == current_agent:
        return {
            "error": f"Retries exhausted ({retry_count}/{max_retries}) "
            f"and no escalation target for agent '{current_agent}'",
        }

    result = escalate_worker_pane(
        project_root,
        slug,
        target_agent=next_agent,
        session_root=session_root,
        permission_mode=permission_mode,
    )
    if result.get("error"):
        return result

    # Reset retry_count on the new (escalated) pane
    set_pane_metadata(session_root, result["new_slug"], retry_count=0)

    return {
        "action": "escalate",
        "agent": next_agent,
        "retry_count": 0,
        "original_slug": slug,
        "new_slug": result["new_slug"],
        "from_agent": current_agent,
    }


def _resolve_escalation_target(current_agent: str, project_root: str) -> str:
    """Determine the next agent for escalation.

    Priority: agent's ``retry_escalate_to`` config > ``ESCALATION_CHAIN`` > same agent.
    """

    registry = load_registry(project_root or None)
    agent_def = registry.get(current_agent)
    if agent_def and agent_def.retry_escalate_to:
        return agent_def.retry_escalate_to

    return ESCALATION_CHAIN.get(current_agent, current_agent)


def _slug_lineage(session_root: str, slug: str) -> list[str]:
    """Walk retried_from links back to the original slug, return all slugs in chain."""
    chain = [slug]
    visited: set[str] = {slug}
    current = slug
    while True:
        rec = get_pane(session_root, current)
        if not rec:
            break
        parent = rec.get("retried_from")
        if not parent or parent in visited:
            break
        chain.append(parent)
        visited.add(parent)
        current = parent
    return chain


def _count_retries(session_root: str, slug: str, events: list[dict] | None = None) -> int:
    """Count pane_auto_retried events for the slug lineage."""
    lineage = set(_slug_lineage(session_root, slug))
    if events is None:
        events = read_events(session_root)
    count = 0
    for ev in events:
        if ev.get("event") == "pane_auto_retried" and ev.get("pane") in lineage:
            count += 1
    return count


def retry_context(slug: str, session_root: str, events: list[dict] | None = None) -> str:
    """Build a failure context string from the pane's last output and events."""
    rec = get_pane(session_root, slug)
    if not rec:
        return ""

    parts: list[str] = []

    # Try to read the pane log
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-20:] if len(lines) > 20 else lines
            if tail:
                parts.append("Last output:\n" + "\n".join(tail))
        except OSError:
            pass

    # Check for exit code
    exit_path = Path(session_root) / STATE_DIR / "done" / f"{slug}.exit"
    if exit_path.exists():
        try:
            code = exit_path.read_text().strip()
            parts.append(f"Exit code: {code}")
        except OSError:
            pass

    # Recent events for this slug
    if events is None:
        events = read_events(session_root)
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
    rec = get_pane(session_root, slug)
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
    rec = get_pane(session_root, slug)
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

        emit_event(
            session_root,
            "pane_auto_retried",
            slug,
            attempt=attempt + 1,
            max_retries=policy.max_retries,
        )

        result = retry_worker_pane(
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
        emit_event(
            session_root,
            "pane_auto_retried",
            slug,
            attempt=attempt + 1,
            escalated_to=policy.escalate_to,
        )

        result = escalate_worker_pane(
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
