"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypedDict

from dgov.context_packet import ContextPacket, build_context_packet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewGate:
    review: dict
    passed: bool
    verdict: str
    commit_count: int
    error: str | None = None


class HandleResult(TypedDict):
    slug: str
    method: str | None
    action: str | None
    new_slug: str | None
    attempt: int | None
    from_agent: str | None
    to_agent: str | None
    error: str | None
    verdict: str | None
    issues: list[str] | None


class ExecutorLifecycle:
    """Canonical executor lifecycle handler for post-dispatch pane behavior.

    This class owns the wait/review/merge lifecycle after dispatch, handling:
    - successful completion transitions
    - review_pending state for non-safe verdicts
    - failed pane auto-retry and escalation
    - timeout recovery with retry then escalation

    All methods work purely on pane records and persistence layer.
    """

    def __init__(self, session_root: str):
        self.session_root = session_root

    def handle_successful_completion(self, slug: str) -> HandleResult:
        """Handle successful pane completion.

        Transitions pane to 'done' state and emits pane_done event.
        Returns result dict with method and slug.
        """
        from dgov.persistence import emit_event, get_pane, update_pane_state

        rec = get_pane(self.session_root, slug)
        if not rec:
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"Pane not found: {slug}",
                "verdict": None,
                "issues": None,
            }

        # Only transition if pane is still running or waiting
        current_state = rec.get("state", "")
        if current_state in ("merged", "done", "failed", "abandoned"):
            # Already complete — just return success indicator
            return {
                "slug": slug,
                "method": "already_complete",
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": None,
                "verdict": current_state,
                "issues": None,
            }

        # Transition to done state
        update_pane_state(self.session_root, slug, "done")
        emit_event(self.session_root, "pane_done", slug)

        return {
            "slug": slug,
            "method": "signal_or_commit",
            "action": None,
            "new_slug": None,
            "attempt": None,
            "from_agent": None,
            "to_agent": None,
            "error": None,
            "verdict": None,
            "issues": None,
        }

    def handle_review_pending(self, slug: str) -> HandleResult:
        """Handle review_pending state for non-safe verdict.

        Transitions pane to 'review_pending' and returns verdict details.
        """
        from dgov.inspection import review_worker_pane
        from dgov.persistence import emit_event, get_pane, update_pane_state

        rec = get_pane(self.session_root, slug)
        if not rec:
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"Pane not found: {slug}",
                "verdict": None,
                "issues": None,
            }

        current_state = rec.get("state", "")
        if current_state in ("review_pending", "failed", "abandoned"):
            # Already in review_pending or terminal state — just return verdict
            review = review_worker_pane(self.session_root, slug)
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": None,
                "verdict": review.get("verdict"),
                "issues": review.get("issues", []),
            }

        # Review the pane to get verdict details
        review = review_worker_pane(self.session_root, slug)
        verdict = review.get("verdict", "unknown")
        issues = review.get("issues", [])

        if issues:
            update_pane_state(self.session_root, slug, "review_pending")
            emit_event(self.session_root, "pane_review_pending", slug, issues=issues)

        return {
            "slug": slug,
            "method": None,
            "action": None,
            "new_slug": None,
            "attempt": None,
            "from_agent": None,
            "to_agent": None,
            "error": None,
            "verdict": verdict,
            "issues": issues,
        }

    def handle_failed_pane(
        self,
        slug: str,
        project_root: str,
    ) -> HandleResult:
        """Handle failed pane with auto-retry or escalation.

        Consults agent's retry policy and triggers automatic recovery.
        """
        from dgov.persistence import get_pane
        from dgov.recovery import maybe_auto_retry

        rec = get_pane(self.session_root, slug)
        if not rec:
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"Pane not found: {slug}",
                "verdict": None,
                "issues": None,
            }

        current_state = rec.get("state", "")
        if current_state not in ("failed", "abandoned"):
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"Pane not in failed/abandoned state: {current_state}",
                "verdict": current_state,
                "issues": None,
            }

        result = maybe_auto_retry(self.session_root, slug, project_root)
        if result is None:
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"No retry policy for agent '{rec.get('agent', 'unknown')}'",
                "verdict": None,
                "issues": None,
            }

        # Build result based on action type
        if result.get("retried"):
            return {
                "slug": slug,
                "method": None,
                "action": "retry",
                "new_slug": result.get("new_slug"),
                "attempt": result.get("attempt"),
                "from_agent": rec.get("agent"),
                "to_agent": rec.get("agent"),
                "error": None,
                "verdict": None,
                "issues": None,
            }

        if result.get("escalated"):
            return {
                "slug": slug,
                "method": None,
                "action": "escalate",
                "new_slug": result.get("new_slug"),
                "attempt": None,
                "from_agent": rec.get("agent"),
                "to_agent": result.get("to"),
                "error": None,
                "verdict": None,
                "issues": None,
            }

        return {
            "slug": slug,
            "method": None,
            "action": None,
            "new_slug": None,
            "attempt": None,
            "from_agent": None,
            "to_agent": None,
            "error": f"Unknown result from maybe_auto_retry: {result}",
            "verdict": None,
            "issues": None,
        }

    def handle_timeout(self, slug: str, project_root: str) -> HandleResult:
        """Handle pane timeout with retry then escalation.

        First attempts retry policy, then escalates if retries exhausted.
        """
        from dgov.persistence import emit_event, get_pane, update_pane_state

        rec = get_pane(self.session_root, slug)
        if not rec:
            return {
                "slug": slug,
                "method": None,
                "action": None,
                "new_slug": None,
                "attempt": None,
                "from_agent": None,
                "to_agent": None,
                "error": f"Pane not found: {slug}",
                "verdict": None,
                "issues": None,
            }

        # Mark as timed_out state (if not already)
        current_state = rec.get("state", "")
        if current_state != "timed_out":
            update_pane_state(self.session_root, slug, "timed_out")
            emit_event(self.session_root, "pane_timed_out", slug)

        # Delegate to maybe_auto_retry for policy-based decision
        return self.handle_failed_pane(slug, project_root)

    def can_merge(self, slug: str) -> bool:
        """Check if a pane's changes can be merged.

        Returns True only if review passes (safe verdict, commits present).
        """
        from dgov.inspection import review_worker_pane

        review = review_worker_pane(self.session_root, slug)
        verdict = review.get("verdict", "unknown")
        commit_count = review.get("commit_count", 0)

        if commit_count == 0:
            return False
        if verdict != "safe":
            return False

        return True


def _dedupe_paths(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(p for p in paths if p))


def derive_prompt_touches(prompt: str) -> list[str]:
    from dgov.strategy import extract_task_context

    context = extract_task_context(prompt)
    return _dedupe_paths(
        [
            *context.get("primary_files", []),
            *context.get("also_check", []),
            *context.get("tests", []),
        ]
    )


def resolve_touches(prompt: str | None = None, touches: list[str] | None = None) -> list[str]:
    if touches is not None:
        return _dedupe_paths(touches)
    if not prompt:
        return []
    return derive_prompt_touches(prompt)


def run_dispatch_preflight(
    project_root: str,
    agent: str,
    *,
    session_root: str | None = None,
    packet: ContextPacket | None = None,
    prompt: str | None = None,
    touches: list[str] | None = None,
    expected_branch: str | None = None,
    skip_deps: bool = True,
):
    from dgov.preflight import run_preflight

    if packet is None:
        packet = build_context_packet(prompt or "", file_claims=touches)

    return run_preflight(
        project_root=project_root,
        agent=agent,
        touches=list(packet.touches),
        expected_branch=expected_branch,
        session_root=session_root,
        skip_deps=skip_deps,
    )


def review_merge_gate(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    full: bool = False,
    require_safe: bool = True,
    require_commits: bool = True,
) -> ReviewGate:
    from dgov.inspection import review_worker_pane

    if full:
        review = review_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            full=True,
        )
    else:
        review = review_worker_pane(
            project_root,
            slug,
            session_root=session_root,
        )
    verdict = review.get("verdict", "unknown")
    commit_count = review.get("commit_count", 0)
    error = review.get("error")
    if error:
        return ReviewGate(
            review=review,
            passed=False,
            verdict=verdict,
            commit_count=commit_count,
            error=error,
        )
    if require_commits and commit_count == 0:
        return ReviewGate(
            review=review,
            passed=False,
            verdict=verdict,
            commit_count=commit_count,
            error="No commits to merge",
        )
    if require_safe and verdict != "safe":
        return ReviewGate(
            review=review,
            passed=False,
            verdict=verdict,
            commit_count=commit_count,
            error=f"Review verdict is {verdict}; refusing to merge",
        )
    return ReviewGate(
        review=review,
        passed=True,
        verdict=verdict,
        commit_count=commit_count,
    )
