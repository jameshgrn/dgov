"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, TypedDict

from dgov.context_packet import ContextPacket, build_context_packet
from dgov.decision import DecisionRecord, ReviewOutputDecision, ReviewOutputRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewGate:
    review: dict
    passed: bool
    verdict: str
    commit_count: int
    error: str | None = None


@dataclass(frozen=True)
class PostDispatchResult:
    state: str
    slug: str
    review: dict | None = None
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    merge_result: dict | None = None
    cleanup: CleanupOnlyResult | None = None
    error: str | None = None
    failure_stage: str | None = None


@dataclass(frozen=True)
class ReviewMergeResult:
    slug: str
    review: dict
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    merge_result: dict | None = None
    failure_stage: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MergeOnlyResult:
    slug: str
    merge_result: dict | None = None
    error: str | None = None


@dataclass(frozen=True)
class LandResult:
    slug: str
    review: dict
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    merge_result: dict | None = None
    cleanup: CleanupOnlyResult | None = None
    failure_stage: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReviewOnlyResult:
    slug: str
    review: dict
    passed: bool
    verdict: str
    commit_count: int
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    error: str | None = None


@dataclass(frozen=True)
class WaitOnlyResult:
    state: str
    slug: str
    wait_result: dict | None = None
    pane_state: str | None = None
    error: str | None = None
    failure_stage: str | None = None


@dataclass(frozen=True)
class CleanupOnlyResult:
    slug: str
    action: str
    reason: str
    closed: bool = False
    force: bool = False
    error: str | None = None


def run_cleanup_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    state: str,
    failure_stage: str | None = None,
) -> CleanupOnlyResult:
    """Run the canonical cleanup policy for a terminal lifecycle outcome."""
    from dgov.lifecycle import close_worker_pane
    from dgov.persistence import mark_preserved_artifacts

    preserve = CleanupOnlyResult(
        slug=slug,
        action="preserve",
        reason=failure_stage or state,
    )

    if state == "failed" and failure_stage in {"timeout", "recovery", "review"}:
        force = False
    elif state == "failed" and failure_stage == "worker_failed":
        force = True
    else:
        if session_root:
            try:
                mark_preserved_artifacts(
                    session_root,
                    slug,
                    reason=failure_stage or state,
                    recoverable=False,
                    state=state,
                    failure_stage=failure_stage,
                )
            except (OSError, sqlite3.Error):
                logger.debug(
                    "Skipping preserved-artifact metadata for %s; session root unavailable",
                    slug,
                    exc_info=True,
                )
        return preserve

    closed = close_worker_pane(
        project_root,
        slug,
        session_root=session_root,
        force=force,
    )
    return CleanupOnlyResult(
        slug=slug,
        action="close",
        reason=failure_stage or state,
        closed=closed,
        force=force,
        error=None if closed else f"Failed to close pane: {slug}",
    )


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
            review = run_review_only(
                self.session_root,
                slug,
                session_root=self.session_root,
                require_safe=False,
                require_commits=False,
            ).review
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
        review = run_review_only(
            self.session_root,
            slug,
            session_root=self.session_root,
            require_safe=False,
            require_commits=False,
        ).review
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
        review = run_review_only(
            self.session_root,
            slug,
            session_root=self.session_root,
        )
        return review.passed


def run_wait_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    timeout: int = 600,
    max_retries: int = 1,
    permission_mode: str = "bypassPermissions",
    retry_agent: str | None = None,
    escalate_to: str | None = None,
    phase_callback: Callable[[str, str], None] | None = None,
) -> WaitOnlyResult:
    """Run the canonical wait and recovery loop without review or merge."""
    import os

    from dgov.persistence import get_pane
    from dgov.recovery import escalate_worker_pane, retry_worker_pane
    from dgov.waiter import PaneTimeoutError, wait_worker_pane

    session_root = os.path.abspath(session_root or project_root)

    def _phase(name: str, current_slug: str) -> None:
        if phase_callback is not None:
            phase_callback(name, current_slug)

    current_slug = slug
    retries_left = max_retries
    wait_result: dict | None = None

    _phase("waiting", current_slug)
    while True:
        try:
            wait_result = wait_worker_pane(
                project_root,
                current_slug,
                session_root=session_root,
                timeout=timeout,
                auto_retry=False,
            )
            break
        except PaneTimeoutError:
            if retries_left <= 0:
                _phase("failed", current_slug)
                return WaitOnlyResult(
                    state="failed",
                    slug=current_slug,
                    error=f"Worker timed out after {timeout}s (retries exhausted)",
                    failure_stage="timeout",
                )

            if escalate_to:
                esc_result = escalate_worker_pane(
                    project_root,
                    current_slug,
                    target_agent=escalate_to,
                    session_root=session_root,
                    permission_mode=permission_mode,
                )
                if esc_result.get("error"):
                    _phase("failed", current_slug)
                    return WaitOnlyResult(
                        state="failed",
                        slug=current_slug,
                        error=f"Escalation failed: {esc_result['error']}",
                        failure_stage="recovery",
                    )
                current_slug = esc_result["new_slug"]
                retries_left -= 1
                _phase("waiting", current_slug)
                continue

            retry_result = retry_worker_pane(
                project_root,
                current_slug,
                session_root=session_root,
                agent=retry_agent,
            )
            if retry_result.get("error"):
                _phase("failed", current_slug)
                return WaitOnlyResult(
                    state="failed",
                    slug=current_slug,
                    error=f"Retry failed: {retry_result['error']}",
                    failure_stage="recovery",
                )
            current_slug = retry_result["new_slug"]
            retries_left -= 1
            _phase("waiting", current_slug)

    pane = get_pane(session_root, current_slug)
    pane_state = pane.get("state") if pane else None
    if pane_state == "failed":
        _phase("failed", current_slug)
        return WaitOnlyResult(
            state="failed",
            slug=current_slug,
            wait_result=wait_result,
            pane_state=pane_state,
            error="Worker exited with an error (check logs with: dgov pane logs)",
            failure_stage="worker_failed",
        )
    if pane_state in ("timed_out", "abandoned"):
        _phase("failed", current_slug)
        return WaitOnlyResult(
            state="failed",
            slug=current_slug,
            wait_result=wait_result,
            pane_state=pane_state,
            error=f"Worker ended in {pane_state} state",
            failure_stage="worker_failed",
        )

    return WaitOnlyResult(
        state="completed",
        slug=current_slug,
        wait_result=wait_result,
        pane_state=pane_state,
    )


def run_post_dispatch_lifecycle(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    timeout: int = 600,
    max_retries: int = 1,
    auto_merge: bool = True,
    permission_mode: str = "bypassPermissions",
    retry_agent: str | None = None,
    escalate_to: str | None = None,
    phase_callback: Callable[[str, str], None] | None = None,
) -> PostDispatchResult:
    """Run the canonical post-dispatch wait/review/merge lifecycle.

    This owns the policy for:
    - waiting for worker completion
    - retry/escalation on timeout
    - worker failure detection
    - review gating
    - merge execution

    Callers can map phase transitions to their own event vocabulary via
    *phase_callback* without re-implementing the lifecycle itself.
    """
    import os

    session_root = os.path.abspath(session_root or project_root)

    def _phase(name: str, current_slug: str) -> None:
        if phase_callback is not None:
            phase_callback(name, current_slug)

    wait = run_wait_only(
        project_root,
        slug,
        session_root=session_root,
        timeout=timeout,
        max_retries=max_retries,
        permission_mode=permission_mode,
        retry_agent=retry_agent,
        escalate_to=escalate_to,
        phase_callback=phase_callback,
    )
    if wait.state != "completed":
        cleanup = run_cleanup_only(
            project_root,
            wait.slug,
            session_root=session_root,
            state="failed",
            failure_stage=wait.failure_stage,
        )
        return PostDispatchResult(
            state="failed",
            slug=wait.slug,
            cleanup=cleanup,
            error=wait.error,
            failure_stage=wait.failure_stage,
        )

    current_slug = wait.slug

    _phase("reviewing", current_slug)
    review = run_review_only(
        project_root,
        current_slug,
        session_root=session_root,
        require_safe=False,
        require_commits=False,
    )
    if review.error:
        _phase("failed", current_slug)
        cleanup = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state="failed",
            failure_stage="review",
        )
        return PostDispatchResult(
            state="failed",
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            cleanup=cleanup,
            error=f"Review failed: {review.error}",
            failure_stage="review",
        )

    if review.verdict != "safe":
        cleanup = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state="review_pending",
        )
        return PostDispatchResult(
            state="review_pending",
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            cleanup=cleanup,
        )

    if review.commit_count == 0:
        _phase("failed", current_slug)
        cleanup = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state="failed",
            failure_stage="review",
        )
        return PostDispatchResult(
            state="failed",
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            cleanup=cleanup,
            error="Review failed: No commits to merge",
            failure_stage="review",
        )

    if not auto_merge:
        cleanup = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state="review_pending",
        )
        return PostDispatchResult(
            state="reviewed_pass",
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            cleanup=cleanup,
        )

    _phase("merging", current_slug)
    merge = run_merge_only(project_root, current_slug, session_root=session_root)
    if merge.error:
        _phase("failed", current_slug)
        cleanup = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state="failed",
            failure_stage="merge",
        )
        return PostDispatchResult(
            state="failed",
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            merge_result=merge.merge_result,
            cleanup=cleanup,
            error=f"Merge failed: {merge.error}",
            failure_stage="merge",
        )

    _phase("completed", current_slug)
    cleanup = run_cleanup_only(
        project_root,
        current_slug,
        session_root=session_root,
        state="completed",
    )
    return PostDispatchResult(
        state="completed",
        slug=current_slug,
        review=review.review,
        review_record=review.review_record,
        merge_result=merge.merge_result,
        cleanup=cleanup,
    )


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


def get_review_provider():
    """Return the active provider for pane review decisions."""
    from dgov.decision_providers import InspectionReviewProvider

    return InspectionReviewProvider()


def review_merge_gate(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    full: bool = False,
    require_safe: bool = True,
    require_commits: bool = True,
) -> ReviewGate:
    review = run_review_only(
        project_root,
        slug,
        session_root=session_root,
        full=full,
        require_safe=require_safe,
        require_commits=require_commits,
    )
    if review.error:
        return ReviewGate(
            review=review.review,
            passed=False,
            verdict=review.verdict,
            commit_count=review.commit_count,
            error=review.error,
        )
    return ReviewGate(
        review=review.review,
        passed=review.passed,
        verdict=review.verdict,
        commit_count=review.commit_count,
    )


def run_review_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    full: bool = False,
    require_safe: bool = True,
    require_commits: bool = True,
) -> ReviewOnlyResult:
    """Run the canonical review operation without merging."""
    provider = get_review_provider()
    record = provider.review_output(
        ReviewOutputRequest(
            project_root=project_root,
            slug=slug,
            session_root=session_root,
            full=full,
        )
    )
    artifact = record.artifact if isinstance(record.artifact, dict) else None
    review = artifact or {
        "slug": slug,
        "verdict": record.decision.verdict,
        "commit_count": record.decision.commit_count,
    }
    if record.decision.issues:
        review.setdefault("issues", list(record.decision.issues))
    if record.decision.reason and "error" not in review:
        review["error"] = record.decision.reason

    verdict = record.decision.verdict
    commit_count = record.decision.commit_count
    error = record.decision.reason
    passed = error is None
    if passed and require_commits and commit_count == 0:
        passed = False
        error = "No commits to merge"
    if passed and require_safe and verdict != "safe":
        passed = False
        error = f"Review verdict is {verdict}; refusing to merge"

    return ReviewOnlyResult(
        slug=slug,
        review=review,
        passed=passed,
        verdict=verdict,
        commit_count=commit_count,
        review_record=record,
        error=error,
    )


def run_review_merge(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
) -> ReviewMergeResult:
    """Run the canonical review gate followed by merge."""
    review = run_review_only(project_root, slug, session_root=session_root)
    if review.review_record and review.review_record.decision.reason is not None:
        return ReviewMergeResult(
            slug=slug,
            review=review.review,
            review_record=review.review_record,
            failure_stage="review_error",
            error=review.error,
        )
    if not review.passed:
        return ReviewMergeResult(
            slug=slug,
            review=review.review,
            review_record=review.review_record,
            failure_stage="review_failed",
            error=review.error or "Review failed",
        )

    merge = run_merge_only(
        project_root,
        slug,
        session_root=session_root,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
    )
    if merge.error:
        return ReviewMergeResult(
            slug=slug,
            review=review.review,
            review_record=review.review_record,
            merge_result=merge.merge_result,
            failure_stage="merge_failed",
            error=merge.error,
        )

    return ReviewMergeResult(
        slug=slug,
        review=review.review,
        review_record=review.review_record,
        merge_result=merge.merge_result,
    )


def run_land_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
) -> LandResult:
    """Run the canonical review, merge, and cleanup flow for a pane."""
    from dgov.lifecycle import close_worker_pane

    result = run_review_merge(
        project_root,
        slug,
        session_root=session_root,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
    )
    if result.error:
        return LandResult(
            slug=slug,
            review=result.review,
            review_record=result.review_record,
            merge_result=result.merge_result,
            failure_stage=result.failure_stage,
            error=result.error,
        )

    closed = close_worker_pane(project_root, slug, session_root=session_root)
    cleanup = CleanupOnlyResult(
        slug=slug,
        action="close" if closed else "preserve",
        reason="landed",
        closed=closed,
        force=False,
        error=None if closed else f"Failed to close pane: {slug}",
    )
    return LandResult(
        slug=slug,
        review=result.review,
        review_record=result.review_record,
        merge_result=result.merge_result,
        cleanup=cleanup,
    )


def run_merge_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
    message: str | None = None,
) -> MergeOnlyResult:
    """Run the canonical merge operation for an already-approved pane."""
    from dgov.merger import merge_worker_pane

    if resolve == "skip" and squash is True and not rebase and message is None:
        merge_result = merge_worker_pane(project_root, slug, session_root=session_root)
    else:
        merge_result = merge_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            resolve=resolve,
            squash=squash,
            message=message,
            rebase=rebase,
        )
    if merge_result.get("error"):
        return MergeOnlyResult(
            slug=slug,
            merge_result=merge_result,
            error=merge_result["error"],
        )
    return MergeOnlyResult(
        slug=slug,
        merge_result=merge_result,
    )
