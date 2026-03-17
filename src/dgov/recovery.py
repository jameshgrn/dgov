"""Pane recovery: escalate, retry, and bounded retry with auto-escalation."""

from __future__ import annotations

import os
import re

from dgov.lifecycle import close_worker_pane, create_worker_pane
from dgov.persistence import (
    all_panes,
    emit_event,
    get_pane,
    set_pane_metadata,
    update_pane_state,
)
from dgov.retry import retry_context

# Default escalation chain: maps an agent to the next-tier agent.
# Terminal agents (codex) map to themselves — no further escalation.
ESCALATION_CHAIN: dict[str, str] = {
    "pi": "claude",
    "hunter": "claude",
    "gemini": "claude",
    "claude": "codex",
    "codex": "codex",
    "cursor": "codex",
}


def escalate_worker_pane(
    project_root: str,
    slug: str,
    target_agent: str = "claude",
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
    from dgov.agents import load_registry

    registry = load_registry(project_root or None)
    agent_def = registry.get(current_agent)
    if agent_def and agent_def.retry_escalate_to:
        return agent_def.retry_escalate_to

    return ESCALATION_CHAIN.get(current_agent, current_agent)
