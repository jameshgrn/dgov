"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

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
    suggest_escalate: bool = False


@dataclass(frozen=True)
class CleanupOnlyResult:
    slug: str
    action: str
    reason: str
    closed: bool = False
    force: bool = False
    error: str | None = None


@dataclass
class PaneFinalizeResult:
    slug: str
    review: dict
    merge_result: dict | None
    error: str | None
    cleanup_error: str | None


@dataclass
class PostDispatchActionExecutor:
    project_root: str
    session_root: str
    timeout: int = 600
    max_retries: int = 1
    permission_mode: str = "bypassPermissions"
    retry_agent: str | None = None
    escalate_to: str | None = None
    resolve: str = "skip"
    squash: bool = True
    rebase: bool = False
    message: str | None = None
    phase_callback: Callable[[str, str], None] | None = None
    wait: WaitOnlyResult | None = None
    review: ReviewOnlyResult | None = None
    merge: MergeOnlyResult | None = None
    cleanup: CleanupOnlyResult | None = None

    def _phase(self, name: str, slug: str) -> None:
        if self.phase_callback is not None:
            self.phase_callback(name, slug)

    def execute(self, action):  # noqa: ANN001
        from dgov.kernel import (
            CleanupCompleted,
            CleanupPane,
            MergeCompleted,
            MergePane,
            ReviewCompleted,
            ReviewPane,
            WaitCompleted,
            WaitForPane,
        )

        if isinstance(action, WaitForPane):
            self.wait = run_wait_only(
                self.project_root,
                action.slug,
                session_root=self.session_root,
                timeout=self.timeout,
                max_retries=self.max_retries,
                permission_mode=self.permission_mode,
                retry_agent=self.retry_agent,
                escalate_to=self.escalate_to,
                phase_callback=self.phase_callback,
            )
            return WaitCompleted(self.wait)

        if isinstance(action, ReviewPane):
            self._phase("reviewing", action.slug)
            self.review = run_review_only(
                self.project_root,
                action.slug,
                session_root=self.session_root,
                require_safe=False,
                require_commits=False,
            )
            return ReviewCompleted(self.review)

        if isinstance(action, MergePane):
            self._phase("merging", action.slug)
            if self.message is None:
                self.merge = run_merge_only(
                    self.project_root,
                    action.slug,
                    session_root=self.session_root,
                    resolve=self.resolve,
                    squash=self.squash,
                    rebase=self.rebase,
                )
            else:
                self.merge = run_merge_only(
                    self.project_root,
                    action.slug,
                    session_root=self.session_root,
                    resolve=self.resolve,
                    squash=self.squash,
                    rebase=self.rebase,
                    message=self.message,
                )
            return MergeCompleted(self.merge)

        if isinstance(action, CleanupPane):
            if action.state == "failed" and (self.wait is None or self.wait.state == "completed"):
                self._phase("failed", action.slug)
            elif action.state == "completed":
                self._phase("completed", action.slug)
            self.cleanup = run_cleanup_only(
                self.project_root,
                action.slug,
                session_root=self.session_root,
                state=action.state,
                failure_stage=action.failure_stage,
            )
            return CleanupCompleted(self.cleanup)

        raise RuntimeError(f"Unhandled kernel action: {action!r}")

    def final_slug(self, initial_slug: str) -> str:
        return (
            self.cleanup.slug
            if self.cleanup is not None
            else self.merge.slug
            if self.merge is not None
            else self.review.slug
            if self.review is not None
            else self.wait.slug
            if self.wait is not None
            else initial_slug
        )


def _drive_post_dispatch_kernel(
    kernel,
    actions,
    runtime: PostDispatchActionExecutor,
    *,
    execute_cleanup: bool,
):
    from dgov.kernel import CleanupPane

    pending = list(actions)
    while pending:
        action = pending.pop(0)
        if isinstance(action, CleanupPane) and not execute_cleanup:
            runtime.cleanup = None
            break
        event = runtime.execute(action)
        pending.extend(kernel.handle(event))


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


def run_wait_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
    max_retries: int = 1,
    auto_retry: bool = True,
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

    def _should_suggest_escalate(slug: str) -> bool:
        from dgov.recovery import _resolve_escalation_target

        try:
            rec = get_pane(session_root, slug)
        except Exception:  # noqa: BLE001
            return False
        if not rec:
            return False
        agent = rec.get("agent", "")
        return bool(agent) and _resolve_escalation_target(agent, project_root) != agent

    _phase("waiting", current_slug)
    while True:
        try:
            wait_result = wait_worker_pane(
                project_root,
                current_slug,
                session_root=session_root,
                timeout=timeout,
                poll=poll,
                stable=stable,
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
                    suggest_escalate=_should_suggest_escalate(current_slug),
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
                        suggest_escalate=_should_suggest_escalate(current_slug),
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
                    suggest_escalate=_should_suggest_escalate(current_slug),
                )
            current_slug = retry_result["new_slug"]
            retries_left -= 1
            _phase("waiting", current_slug)

    pane = get_pane(session_root, current_slug)
    pane_state = pane.get("state") if pane else None
    if pane_state == "failed":
        if auto_retry and retries_left > 0:
            from dgov.recovery import maybe_auto_retry

            retry_result = maybe_auto_retry(session_root, current_slug, project_root)
            if retry_result and retry_result.get("new_slug"):
                current_slug = retry_result["new_slug"]
                retries_left -= 1
                wait_result = None
                _phase("waiting", current_slug)
                # loop back to wait on the new pane
                try:
                    wait_result = wait_worker_pane(
                        project_root,
                        current_slug,
                        session_root=session_root,
                        timeout=timeout,
                        poll=poll,
                        stable=stable,
                        auto_retry=False,
                    )
                except PaneTimeoutError:
                    _phase("failed", current_slug)
                    return WaitOnlyResult(
                        state="failed",
                        slug=current_slug,
                        error=f"Retried pane timed out after {timeout}s",
                        failure_stage="timeout",
                        suggest_escalate=_should_suggest_escalate(current_slug),
                    )
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
                suggest_escalate=_should_suggest_escalate(current_slug),
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
            suggest_escalate=_should_suggest_escalate(current_slug),
        )

    return WaitOnlyResult(
        state="completed",
        slug=current_slug,
        wait_result=wait_result,
        pane_state=pane_state,
    )


def run_wait_all(
    project_root: str,
    *,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
):
    """Yield completion results for every active worker pane."""
    import os

    from dgov.waiter import wait_all_worker_panes

    session_root = os.path.abspath(session_root or project_root)
    yield from wait_all_worker_panes(
        project_root,
        session_root=session_root,
        timeout=timeout,
        poll=poll,
        stable=stable,
    )


def run_post_dispatch_lifecycle(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    timeout: int = 600,
    max_retries: int = 1,
    auto_merge: bool = True,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
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

    from dgov.kernel import (
        KernelState,
        PostDispatchKernel,
    )

    kernel = PostDispatchKernel(auto_merge=auto_merge)
    actions = kernel.start(slug)
    runtime = PostDispatchActionExecutor(
        project_root=project_root,
        session_root=session_root,
        timeout=timeout,
        max_retries=max_retries,
        permission_mode=permission_mode,
        retry_agent=retry_agent,
        escalate_to=escalate_to,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
        phase_callback=phase_callback,
    )
    _drive_post_dispatch_kernel(kernel, actions, runtime, execute_cleanup=True)

    current_slug = runtime.final_slug(slug)
    wait = runtime.wait
    review = runtime.review
    merge = runtime.merge
    cleanup = runtime.cleanup

    if kernel.state is KernelState.FAILED:
        failure_stage = wait.failure_stage if wait and wait.state != "completed" else None
        error = wait.error if wait and wait.state != "completed" else None
        if review is not None and review.error is not None:
            failure_stage = "review"
            error = f"Review failed: {review.error}"
        elif review is not None and review.commit_count == 0:
            failure_stage = "review"
            error = "Review failed: No commits to merge"
        elif merge is not None and merge.error is not None:
            failure_stage = "merge"
            error = f"Merge failed: {merge.error}"

        return PostDispatchResult(
            state="failed",
            slug=current_slug,
            review=None if review is None else review.review,
            review_record=None if review is None else review.review_record,
            merge_result=None if merge is None else merge.merge_result,
            cleanup=cleanup,
            error=error,
            failure_stage=failure_stage,
        )

    if kernel.state is KernelState.REVIEW_PENDING:
        return PostDispatchResult(
            state="review_pending",
            slug=current_slug,
            review=None if review is None else review.review,
            review_record=None if review is None else review.review_record,
            cleanup=cleanup,
        )

    if kernel.state is KernelState.REVIEWED_PASS:
        return PostDispatchResult(
            state="reviewed_pass",
            slug=current_slug,
            review=None if review is None else review.review,
            review_record=None if review is None else review.review_record,
            cleanup=cleanup,
        )

    if kernel.state is KernelState.COMPLETED:
        return PostDispatchResult(
            state="completed",
            slug=current_slug,
            review=None if review is None else review.review,
            review_record=None if review is None else review.review_record,
            merge_result=None if merge is None else merge.merge_result,
            cleanup=cleanup,
        )

    raise RuntimeError(f"Unexpected terminal kernel state: {kernel.state}")


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


def get_review_provider(session_root: str | None = None):
    """Return the active provider for pane review decisions."""
    from dgov.provider_registry import get_review_provider as _get_provider

    return _get_provider(session_root=session_root)


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
    provider = get_review_provider(session_root=session_root)
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
    import os

    from dgov.kernel import KernelState, PostDispatchKernel

    session_root = os.path.abspath(session_root or project_root)
    kernel = PostDispatchKernel(auto_merge=True)
    runtime = PostDispatchActionExecutor(
        project_root=project_root,
        session_root=session_root,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
        phase_callback=None,
    )
    _drive_post_dispatch_kernel(
        kernel,
        kernel.start_review(slug),
        runtime,
        execute_cleanup=False,
    )

    current_slug = runtime.final_slug(slug)
    review = runtime.review
    merge_result = runtime.merge.merge_result if runtime.merge is not None else None

    if review is None:
        return ReviewMergeResult(
            slug=current_slug,
            review={"slug": current_slug, "error": "review not executed"},
            failure_stage="review_error",
            error="Review failed",
        )

    if kernel.state is KernelState.FAILED:
        if review.error is not None:
            return ReviewMergeResult(
                slug=current_slug,
                review=review.review,
                review_record=review.review_record,
                failure_stage="review_error",
                error=review.error,
            )
        if review.commit_count == 0:
            return ReviewMergeResult(
                slug=current_slug,
                review=review.review,
                review_record=review.review_record,
                failure_stage="review_failed",
                error="No commits to merge",
            )
        if runtime.merge is not None and runtime.merge.error is not None:
            return ReviewMergeResult(
                slug=current_slug,
                review=review.review,
                review_record=review.review_record,
                merge_result=merge_result,
                failure_stage="merge_failed",
                error=runtime.merge.error,
            )

    if kernel.state is KernelState.REVIEW_PENDING:
        error = review.error
        if error is None:
            error = f"Review verdict is {review.verdict}; refusing to merge"
        return ReviewMergeResult(
            slug=current_slug,
            review=review.review,
            review_record=review.review_record,
            failure_stage="review_failed",
            error=error,
        )

    return ReviewMergeResult(
        slug=current_slug,
        review=review.review,
        review_record=review.review_record,
        merge_result=merge_result,
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

    # Delegate cleanup to run_cleanup_only with state='completed'
    # Note: run_cleanup_only preserves on completed (for lifecycle),
    # so we override with actual close logic for land-only context
    from dgov.lifecycle import close_worker_pane

    cleanup = run_cleanup_only(project_root, slug, session_root=session_root, state="completed")
    if cleanup.action == "preserve":
        closed = close_worker_pane(project_root, slug, session_root=session_root)
        cleanup = CleanupOnlyResult(
            slug=slug,
            action="close",
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


def run_finalize_panes(
    project_root: str,
    slugs: list[str],
    *,
    session_root: str | None = None,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
    timeout: int = 600,
    max_retries: int = 1,
    permission_mode: str = "bypassPermissions",
    retry_agent: str | None = None,
    escalate_to: str | None = None,
    close: bool = True,
) -> list[PaneFinalizeResult]:
    """Run lifecycle operations (review+merge +/- cleanup) for multiple panes."""
    if not slugs:
        return []

    import os

    session_root = os.path.abspath(session_root or project_root)
    results: list[PaneFinalizeResult] = []

    for slug in slugs:
        if close:
            lifecycle = run_post_dispatch_lifecycle(
                project_root,
                slug,
                session_root=session_root,
                timeout=timeout,
                max_retries=max_retries,
                permission_mode=permission_mode,
                retry_agent=retry_agent,
                escalate_to=escalate_to,
                resolve=resolve,
                squash=squash,
                rebase=rebase,
            )
            cleanup_error = (
                lifecycle.cleanup.error if lifecycle.cleanup and lifecycle.cleanup.error else None
            )
            results.append(
                PaneFinalizeResult(
                    slug=lifecycle.slug,
                    review=lifecycle.review or {},
                    merge_result=lifecycle.merge_result,
                    error=lifecycle.error,
                    cleanup_error=cleanup_error,
                )
            )
        else:
            result = run_review_merge(
                project_root,
                slug,
                session_root=session_root,
                resolve=resolve,
                squash=squash,
                rebase=rebase,
            )
            results.append(
                PaneFinalizeResult(
                    slug=result.slug,
                    review=result.review,
                    merge_result=result.merge_result,
                    error=result.error,
                    cleanup_error=None,
                )
            )

    return results


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
