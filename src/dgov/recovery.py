"""Pane recovery: escalate and retry."""

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

    # Create the new pane first, then close the old one
    new_slug = f"{slug}-esc"
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
    permission_mode: str = "acceptEdits",
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
