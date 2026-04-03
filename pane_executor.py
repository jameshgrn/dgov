"""Pane executor: single-pane state management functions.

Extracted from executor.py to provide a dedicated module for pane lifecycle
operations without the heavy dependencies of the full executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.persistence import (
    STATE_DIR,
    PaneState,
    clear_preserved_artifacts,
    emit_event,
    get_pane,
    settle_completion_state,
    set_pane_metadata,
    update_pane_state,
)

if TYPE_CHECKING:
    from dgov.persistence import CompletionTransitionResult

logger = logging.getLogger(__name__)


class CleanupAction(StrEnum):
    """Action taken during cleanup."""

    CLOSED = "closed"
    PRESERVED = "preserved"
    ALREADY_CLOSED = "already_closed"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CleanupOnlyResult:
    """Result of a cleanup-only operation."""

    slug: str
    action: CleanupAction
    cleaned: bool = False
    skipped_worktree: bool = False
    branch_kept: bool = False
    worktree_removal_failed: bool = False
    error: str | None = None

    @property
    def closed(self) -> bool:
        """True if the pane was closed or already closed."""
        return self.action in (CleanupAction.CLOSED, CleanupAction.ALREADY_CLOSED)


@dataclass(frozen=True)
class StateTransitionResult:
    """Result of a pane state transition."""

    slug: str
    previous_state: str
    new_state: str
    changed: bool
    error: str | None = None


@dataclass(frozen=True)
class CloseOnlyResult:
    """Result of a close-only operation."""

    slug: str
    closed: bool
    error: str | None = None


def run_complete_pane(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    reason: str = "complete",
    allow_abandoned: bool = False,
) -> StateTransitionResult:
    """Mark a pane as completed (done).

    Args:
        project_root: Git repo root (kept for API compatibility).
        slug: Pane slug.
        session_root: Session root directory. Defaults to project_root.
        reason: Reason for completion.
        allow_abandoned: Whether to allow transition from abandoned state.

    Returns:
        StateTransitionResult with transition details.
    """
    import os

    session_root = session_root or project_root
    pane = get_pane(session_root, slug)

    if pane is None:
        return StateTransitionResult(
            slug=slug,
            previous_state="unknown",
            new_state=PaneState.DONE,
            changed=False,
            error=f"Pane not found: {slug}",
        )

    previous_state = pane.get("state", PaneState.ACTIVE)

    try:
        result = settle_completion_state(
            session_root,
            slug,
            PaneState.DONE,
            allow_abandoned=allow_abandoned,
        )
        if result.changed:
            emit_event(session_root, "pane_done", slug, reason=reason)
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=result.state,
            changed=result.changed,
        )
    except Exception as exc:
        logger.error("Failed to complete pane %s: %s", slug, exc)
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=PaneState.DONE,
            changed=False,
            error=str(exc),
        )


def run_fail_pane(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    reason: str = "failed",
    allow_abandoned: bool = False,
) -> StateTransitionResult:
    """Mark a pane as failed.

    Args:
        project_root: Git repo root (kept for API compatibility).
        slug: Pane slug.
        session_root: Session root directory. Defaults to project_root.
        reason: Reason for failure.
        allow_abandoned: Whether to allow transition from abandoned state.

    Returns:
        StateTransitionResult with transition details.
    """
    import os

    session_root = session_root or project_root
    pane = get_pane(session_root, slug)

    if pane is None:
        return StateTransitionResult(
            slug=slug,
            previous_state="unknown",
            new_state=PaneState.FAILED,
            changed=False,
            error=f"Pane not found: {slug}",
        )

    previous_state = pane.get("state", PaneState.ACTIVE)

    try:
        result = settle_completion_state(
            session_root,
            slug,
            PaneState.FAILED,
            allow_abandoned=allow_abandoned,
        )
        if result.changed:
            emit_event(session_root, "pane_failed", slug, reason=reason)
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=result.state,
            changed=result.changed,
        )
    except Exception as exc:
        logger.error("Failed to fail pane %s: %s", slug, exc)
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=PaneState.FAILED,
            changed=False,
            error=str(exc),
        )


def run_mark_reviewed(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    passed: bool = True,
) -> StateTransitionResult:
    """Mark a pane as reviewed (pass or fail).

    Args:
        project_root: Git repo root (kept for API compatibility).
        slug: Pane slug.
        session_root: Session root directory. Defaults to project_root.
        passed: True for pass, False for fail.

    Returns:
        StateTransitionResult with transition details.
    """
    import os

    session_root = session_root or project_root
    pane = get_pane(session_root, slug)

    if pane is None:
        target_state = PaneState.REVIEWED_PASS if passed else PaneState.REVIEWED_FAIL
        return StateTransitionResult(
            slug=slug,
            previous_state="unknown",
            new_state=target_state,
            changed=False,
            error=f"Pane not found: {slug}",
        )

    previous_state = pane.get("state", PaneState.DONE)
    target_state = PaneState.REVIEWED_PASS if passed else PaneState.REVIEWED_FAIL

    try:
        update_pane_state(session_root, slug, target_state)
        emit_event(
            session_root,
            "pane_reviewed_pass" if passed else "pane_reviewed_fail",
            slug,
        )
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=target_state,
            changed=True,
        )
    except Exception as exc:
        logger.error("Failed to mark pane %s as reviewed: %s", slug, exc)
        return StateTransitionResult(
            slug=slug,
            previous_state=previous_state,
            new_state=target_state,
            changed=False,
            error=str(exc),
        )


def run_cleanup_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    force: bool = False,
    preserve_worktree: bool = False,
) -> CleanupOnlyResult:
    """Run cleanup for a pane without full close.

    Args:
        project_root: Git repo root.
        slug: Pane slug.
        session_root: Session root directory. Defaults to project_root.
        force: Force cleanup even if worktree is dirty.
        preserve_worktree: Keep worktree for inspection.

    Returns:
        CleanupOnlyResult with cleanup details.
    """
    import os
    import subprocess

    session_root = session_root or project_root
    pane = get_pane(session_root, slug)

    if pane is None:
        return CleanupOnlyResult(
            slug=slug,
            action=CleanupAction.NOT_FOUND,
            cleaned=False,
            error=f"Pane not found: {slug}",
        )

    pane_state = pane.get("state", "")

    # Already closed
    if pane_state == PaneState.CLOSED:
        return CleanupOnlyResult(
            slug=slug,
            action=CleanupAction.ALREADY_CLOSED,
            cleaned=True,
        )

    # Clean up done/exit signals
    done_path = Path(session_root) / STATE_DIR / "done" / slug
    done_path.unlink(missing_ok=True)
    exit_path = Path(session_root) / STATE_DIR / "done" / f"{slug}.exit"
    exit_path.unlink(missing_ok=True)

    skipped_worktree = False
    branch_kept = False
    worktree_removal_failed = False

    # Check if we should remove worktree
    if not preserve_worktree and pane.get("owns_worktree", False):
        wt = pane.get("worktree_path", "")
        branch = pane.get("branch_name", "")

        if wt and Path(wt).exists():
            # Check for dirty worktree
            if not force:
                check = subprocess.run(
                    ["git", "-C", wt, "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                )
                if check.stdout.strip():
                    skipped_worktree = True

            if not skipped_worktree:
                # Remove worktree
                remove_result = subprocess.run(
                    ["git", "-C", project_root, "worktree", "remove", "--force", wt],
                    capture_output=True,
                    text=True,
                )
                if remove_result.returncode != 0:
                    logger.error(
                        "Failed to remove worktree %s: %s",
                        wt,
                        remove_result.stderr.strip(),
                    )
                    worktree_removal_failed = True

                # Remove branch
                if branch and not worktree_removal_failed:
                    br_result = subprocess.run(
                        ["git", "-C", project_root, "branch", "-d", branch],
                        capture_output=True,
                        text=True,
                    )
                    if br_result.returncode != 0:
                        # Try force delete
                        br_result = subprocess.run(
                            ["git", "-C", project_root, "branch", "-D", branch],
                            capture_output=True,
                            text=True,
                        )
                        if br_result.returncode != 0:
                            branch_kept = True
                            logger.warning(
                                "Branch %s kept: %s",
                                branch,
                                br_result.stderr.strip(),
                            )

    return CleanupOnlyResult(
        slug=slug,
        action=CleanupAction.CLOSED,
        cleaned=True,
        skipped_worktree=skipped_worktree,
        branch_kept=branch_kept,
        worktree_removal_failed=worktree_removal_failed,
    )


def run_close_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    force: bool = False,
) -> CloseOnlyResult:
    """Close a pane (state transition only, no cascade).

    For full close with cascade and worktree cleanup, use lifecycle.close_worker_pane.

    Args:
        project_root: Git repo root.
        slug: Pane slug.
        session_root: Session root directory. Defaults to project_root.
        force: Force close even if in active state.

    Returns:
        CloseOnlyResult with close details.
    """
    import os

    session_root = session_root or project_root
    pane = get_pane(session_root, slug)

    if pane is None:
        # Check if it was already archived
        from dgov.persistence import read_events

        events = read_events(session_root, slug=slug)
        if events:
            return CloseOnlyResult(slug=slug, closed=True, error=None)
        return CloseOnlyResult(slug=slug, closed=False, error=f"Pane not found: {slug}")

    pane_state = pane.get("state", "")

    # Can't close active panes without force
    if pane_state == PaneState.ACTIVE and not force:
        return CloseOnlyResult(
            slug=slug,
            closed=False,
            error=f"Pane {slug} is active. Use force=True to close.",
        )

    try:
        update_pane_state(session_root, slug, PaneState.CLOSED)
        emit_event(session_root, "pane_closed", slug)
        return CloseOnlyResult(slug=slug, closed=True)
    except Exception as exc:
        logger.error("Failed to close pane %s: %s", slug, exc)
        return CloseOnlyResult(slug=slug, closed=False, error=str(exc))


def run_worker_checkpoint(session_root: str, slug: str, message: str) -> dict:
    """Record a worker checkpoint.

    Args:
        session_root: Session root directory.
        slug: Pane slug.
        message: Checkpoint message.

    Returns:
        Dict with checkpoint details.
    """
    import json
    import time

    checkpoint = {
        "ts": time.time(),
        "message": message,
    }

    # Write to progress file
    progress_dir = Path(session_root) / STATE_DIR / "progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_file = progress_dir / f"{slug}.json"

    try:
        progress_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write checkpoint for %s: %s", slug, exc)

    # Also set as pane metadata
    try:
        set_pane_metadata(session_root, slug, last_checkpoint=checkpoint)
    except Exception as exc:
        logger.warning("Failed to set checkpoint metadata for %s: %s", slug, exc)

    emit_event(session_root, "checkpoint_created", slug, message=message)

    return {"slug": slug, "checkpoint": checkpoint}
