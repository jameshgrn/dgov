"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import json
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


def run_dispatch_only(
    project_root: str,
    prompt: str,
    agent: str,
    *,
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
    slug: str | None = None,
    env_vars: dict[str, str] | None = None,
    extra_flags: str = "",
    existing_worktree: str | None = None,
    skip_auto_structure: bool = False,
    role: str = "worker",
    parent_slug: str = "",
    context_packet: object | None = None,
) -> object:
    """Executor syscall: dispatch a worker pane without full lifecycle.

    This is the canonical entrypoint for bare dispatch operations that should
    not trigger the post-dispatch wait/review/merge pipeline.

    Args:
        project_root: Git repo root.
        prompt: Task prompt for the worker.
        agent: Agent identifier (logical name like qwen-35b).
        session_root: Session root directory.
        permission_mode: Permission mode for the worker.
        slug: Optional custom slug; auto-generated if not provided.
        env_vars: Environment variables to set in the pane.
        extra_flags: Extra flags for the agent CLI.
        existing_worktree: Reuse existing worktree instead of creating new one.
        skip_auto_structure: Skip pi prompt auto-structure.
        role: Pane role (worker or lt-gov).
        parent_slug: Parent pane slug for LT-GOV-created workers.
        context_packet: Context packet with file claims and task context.

    Returns:
        The created pane record (from create_worker_pane).
    """
    from dgov.lifecycle import create_worker_pane

    return create_worker_pane(
        project_root=project_root,
        prompt=prompt,
        agent=agent,
        permission_mode=permission_mode,
        slug=slug,
        env_vars=env_vars,
        extra_flags=extra_flags,
        session_root=session_root,
        existing_worktree=existing_worktree,
        skip_auto_structure=skip_auto_structure,
        role=role,
        parent_slug=parent_slug,
        context_packet=context_packet,
    )


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
    elif state == "closed":
        # 0-commit completed panes: close cleanly (captures transcripts)
        session_root = session_root or project_root
        close_worker_pane(project_root, slug, session_root, force=True)
        return CleanupOnlyResult(slug=slug, action="closed", reason="completed_no_commits")
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
    poll: int = 1,
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

    # Open wait span
    _wait_span_id = None
    try:
        from dgov.spans import SpanKind, open_span

        _wait_span_id = open_span(session_root, slug, SpanKind.WAIT)
    except Exception:
        logger.debug("failed to open wait span for %s", slug, exc_info=True)

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
            # Try to read exit code from .exit file for a better error message
            exit_msg = "Worker exited with an error"
            try:
                from pathlib import Path as _Path

                from dgov.persistence import STATE_DIR

                exit_path = _Path(session_root) / STATE_DIR / "done" / (current_slug + ".exit")
                if exit_path.exists():
                    code = exit_path.read_text().strip()
                    exit_msg = f"Worker exited with code {code}"
            except Exception:
                logger.debug("failed to read exit code for %s", current_slug, exc_info=True)
            return WaitOnlyResult(
                state="failed",
                slug=current_slug,
                wait_result=wait_result,
                pane_state=pane_state,
                error=f"{exit_msg} (check logs with: dgov pane output {current_slug})",
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

    _wait_result = WaitOnlyResult(
        state="completed",
        slug=current_slug,
        wait_result=wait_result,
        pane_state=pane_state,
    )
    # Close wait span
    if _wait_span_id is not None:
        try:
            from dgov.spans import SpanOutcome, close_span

            _wo = SpanOutcome.SUCCESS if _wait_result.state == "completed" else SpanOutcome.FAILURE
            close_span(
                session_root,
                _wait_span_id,
                _wo,
                wait_method=(_wait_result.wait_result or {}).get("method", ""),
            )
        except Exception:
            logger.debug("failed to close wait span", exc_info=True)
    return _wait_result


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


def run_wait_slugs(
    session_root: str,
    slugs: list[str],
    timeout: int = 600,
    poll: int = 3,
    stable_seconds: int | None = None,
) -> set[str]:
    """Executor syscall: wait for a set of slugs to finish.

    Returns the set of slugs still pending at timeout (empty if all completed).

    Args:
        session_root: Session root directory.
        slugs: List of pane slugs to wait for.
        timeout: Maximum seconds to wait.
        poll: Polling interval in seconds.
        stable_seconds: Seconds to wait for stable state before marking done.

    Returns:
        Set of slugs that are still pending after timeout (or empty set).
    """
    from dgov.waiter import wait_for_slugs

    return wait_for_slugs(
        session_root=session_root,
        slugs=slugs,
        timeout=timeout,
        poll=poll,
        stable_seconds=stable_seconds,
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

    # Claim landing lock so the monitor skips this pane
    from dgov.persistence import set_pane_metadata

    try:
        set_pane_metadata(session_root, slug, landing=True)
    except Exception:
        logger.debug("failed to set landing flag for %s", slug, exc_info=True)

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
    try:
        _drive_post_dispatch_kernel(kernel, actions, runtime, execute_cleanup=True)
    finally:
        try:
            set_pane_metadata(session_root, slug, landing=False)
        except Exception:
            logger.debug("failed to unset landing flag for %s", slug, exc_info=True)

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
    """Infer file claims from prompt keywords. Tests excluded — they're read context."""
    from dgov.strategy import extract_task_context

    context = extract_task_context(prompt)
    return _dedupe_paths(
        [
            *context.get("primary_files", []),
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
    review_agent: str = "",
) -> ReviewOnlyResult:
    """Run the canonical review operation without merging."""
    _review_span_id = None
    try:
        from dgov.spans import SpanKind, open_span

        _review_span_id = open_span(session_root or "", slug, SpanKind.REVIEW)
    except Exception:
        logger.debug("failed to open review span for %s", slug, exc_info=True)

    from dgov.decision import DecisionKind, ProviderError

    # Get agent_id from pane if available
    agent_id = None
    if session_root:
        try:
            from dgov.persistence import get_pane

            pane = get_pane(session_root, slug)
            agent_id = pane.get("agent") if pane else None
        except (OSError, Exception):
            pass

    request = ReviewOutputRequest(
        project_root=project_root,
        slug=slug,
        session_root=session_root,
        full=full,
        agent_id=agent_id,
        review_agent=review_agent,
    )

    # Stage 1: Deterministic inspection (always runs, free)
    from dgov.provider_registry import get_provider

    provider = get_provider(DecisionKind.REVIEW_OUTPUT, session_root=session_root)
    record = provider.review_output(request)

    # Stage 2: Model review (only if deterministic passed AND review_agent is set)
    if review_agent and record.decision.verdict == "safe" and record.decision.commit_count > 0:
        try:
            from dgov.decision_providers import ModelReviewProvider

            model_provider = ModelReviewProvider()
            model_record = model_provider.review_output(request)
            if model_record.decision.verdict != "safe":
                record = model_record
                logger.info(
                    "Model review (%s) flagged concerns for %s: %s",
                    review_agent,
                    slug,
                    model_record.decision.issues,
                )
        except ProviderError:
            logger.debug("Model review failed for %s, using deterministic result", slug)
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
        # Check if worker explicitly signaled done (0-commit is intentional)
        _pane_state = None
        if session_root:
            try:
                from dgov.persistence import get_pane as _get_pane

                _p = _get_pane(session_root, slug)
                _pane_state = _p.get("state") if _p else None
            except Exception:
                logger.debug("failed to get pane state for %s", slug, exc_info=True)
        if _pane_state not in ("done", "merged"):
            passed = False
            error = "No commits to merge"
    if passed and require_safe and verdict != "safe":
        passed = False
        error = f"Review verdict is {verdict}; refusing to merge"

    # Build semantic manifest and check for claim violations / staleness
    if passed and commit_count > 0 and session_root:
        import os

        from dgov.kernel import build_manifest_on_completion, validate_manifest_freshness
        from dgov.persistence import get_pane

        sr = os.path.abspath(session_root)
        try:
            pane = get_pane(sr, slug)
        except (OSError, Exception):
            pane = None
        if pane:
            base_sha = pane.get("base_sha", "")
            file_claims = tuple(pane.get("file_claims", ()) or ())
            wt = pane.get("worktree_path", "")
            manifest_root = wt if wt else project_root
            manifest = build_manifest_on_completion(
                manifest_root, slug, base_sha, file_claims=file_claims
            )
            if manifest.claim_violations:
                review["claim_violations"] = list(manifest.claim_violations)
                logger.info(
                    "Claim violations for %s: %s",
                    slug,
                    manifest.claim_violations,
                )
            is_fresh, stale_files = validate_manifest_freshness(project_root, manifest)
            if not is_fresh:
                review["stale_files"] = stale_files
                review["freshness"] = "warn"
                logger.warning(
                    "Stale dependency for %s: main changed %s since base (will attempt merge)",
                    slug,
                    stale_files,
                )

    # Check test coverage for changed source files
    if passed and commit_count > 0 and session_root:
        from dgov.inspection import check_test_coverage

        changed = review.get("changed_files", [])
        if changed:
            missing_tests = check_test_coverage(changed, session_root=session_root)
            if missing_tests:
                review["missing_test_coverage"] = missing_tests
                passed = False
                error = f"Source files changed without test coverage: {', '.join(missing_tests)}"
                logger.info("Test coverage gate failed for %s: %s", slug, missing_tests)

    _review_result = ReviewOnlyResult(
        slug=slug,
        review=review,
        passed=passed,
        verdict=verdict,
        commit_count=commit_count,
        review_record=record,
        error=error,
    )
    if _review_span_id is not None:
        try:
            from dgov.spans import SpanOutcome, close_span

            _ro = SpanOutcome.SUCCESS if passed else SpanOutcome.FAILURE
            close_span(
                session_root or "",
                _review_span_id,
                _ro,
                agent=agent_id or "",
                verdict=verdict,
                commit_count=commit_count,
                tests_passed=1
                if review.get("tests_passed")
                else (0 if review.get("tests_passed") is False else -1),
                stale_files=json.dumps(review.get("stale_files", [])),
                error=error or "",
            )
        except Exception:
            logger.debug("failed to close review span for %s", slug, exc_info=True)
    return _review_result


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
    import os

    from dgov.persistence import get_pane

    sr = os.path.abspath(session_root) if session_root else os.path.abspath(project_root)
    if not get_pane(sr, slug):
        return LandResult(slug=slug, error=f"Pane not found: {slug}", failure_stage="land")

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

    from dgov.persistence import get_pane

    for slug in slugs:
        if not get_pane(session_root, slug):
            results.append(
                PaneFinalizeResult(
                    slug=slug,
                    review={},
                    merge_result=None,
                    error=f"Pane not found: {slug}",
                    cleanup_error=None,
                )
            )
            continue
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
    strict_claims: bool = False,
) -> MergeOnlyResult:
    """Run the canonical merge operation for an already-approved pane."""
    _merge_span_id = None
    try:
        from dgov.spans import SpanKind, open_span

        open_sr = session_root or ""
        _merge_span_id = open_span(
            open_sr, slug, SpanKind.MERGE, merge_strategy="rebase" if rebase else "squash"
        )
    except Exception:
        logger.debug("failed to open merge span for %s", slug, exc_info=True)

    from dgov.merger import merge_worker_pane

    if resolve == "skip" and squash is True and not rebase and message is None:
        merge_result = merge_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            strict_claims=strict_claims,
        )
    else:
        merge_result = merge_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            resolve=resolve,
            squash=squash,
            message=message,
            rebase=rebase,
            strict_claims=strict_claims,
        )

    _merge_error = merge_result.get("error", "")
    _merge_out = MergeOnlyResult(
        slug=slug,
        merge_result=merge_result,
        error=_merge_error if _merge_error else None,
    )

    if _merge_span_id is not None:
        try:
            from dgov.spans import SpanOutcome, close_span

            _mo = SpanOutcome.FAILURE if _merge_error else SpanOutcome.SUCCESS
            close_span(
                session_root or "",
                _merge_span_id,
                _mo,
                files_changed=merge_result.get("files_changed", 0),
                error=_merge_error or "",
            )
        except Exception:
            logger.debug("failed to close merge span", exc_info=True)

    return _merge_out


# -- New executor syscalls: close, retry, escalate, complete, fail --


@dataclass(frozen=True)
class RetryResult:
    slug: str
    new_slug: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class EscalateResult:
    slug: str
    new_slug: str | None = None
    target_agent: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class StateTransitionResult:
    slug: str
    new_state: str
    changed: bool = True
    error: str | None = None


def run_close_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    force: bool = False,
) -> CleanupOnlyResult:
    """Executor syscall: close a pane and reclaim resources."""
    from dgov.lifecycle import close_worker_pane

    closed = close_worker_pane(project_root, slug, session_root=session_root, force=force)
    return CleanupOnlyResult(
        slug=slug,
        action="close",
        reason="explicit_close",
        closed=closed,
        force=force,
        error=None if closed else f"Failed to close pane: {slug}",
    )


def run_retry_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    agent: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> RetryResult:
    """Executor syscall: retry a failed pane with a new worker."""
    from dgov.recovery import retry_worker_pane

    result = retry_worker_pane(
        project_root,
        slug,
        session_root=session_root,
        agent=agent,
    )
    _retry_out = RetryResult(
        slug=slug,
        new_slug=result.get("new_slug"),
        error=result.get("error"),
    )
    try:
        from dgov.spans import SpanKind, SpanOutcome, close_span, open_span

        sid = open_span(
            session_root or "", slug, SpanKind.RETRY, from_agent=agent or "", to_agent=agent or ""
        )
        _ro = SpanOutcome.FAILURE if _retry_out.error else SpanOutcome.SUCCESS
        close_span(session_root or "", sid, _ro, error=_retry_out.error or "")
    except Exception:
        logger.debug("failed to close retry span for %s", slug, exc_info=True)
    return _retry_out


def run_escalate_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    target_agent: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> EscalateResult:
    """Executor syscall: escalate a pane to a stronger agent."""
    from dgov.recovery import escalate_worker_pane

    if target_agent is None:
        from dgov.recovery import _resolve_escalation_target

        pane = None
        if session_root:
            from dgov.persistence import get_pane

            pane = get_pane(session_root, slug)
        current_agent = pane.get("agent", "") if pane else ""
        target_agent = _resolve_escalation_target(current_agent, project_root)
        if target_agent == current_agent:
            return EscalateResult(
                slug=slug,
                error=f"No escalation target for agent {current_agent}",
            )

    result = escalate_worker_pane(
        project_root,
        slug,
        target_agent=target_agent,
        session_root=session_root,
        permission_mode=permission_mode,
    )
    _esc_out = EscalateResult(
        slug=slug,
        new_slug=result.get("new_slug"),
        target_agent=target_agent,
        error=result.get("error"),
    )
    try:
        from dgov.spans import SpanKind, SpanOutcome, close_span, open_span

        sid = open_span(
            session_root or "",
            slug,
            SpanKind.ESCALATE,
            from_agent=current_agent if "current_agent" in dir() else "",
            to_agent=target_agent or "",
        )
        _eo = SpanOutcome.FAILURE if _esc_out.error else SpanOutcome.SUCCESS
        close_span(session_root or "", sid, _eo, error=_esc_out.error or "")
    except Exception:
        logger.debug("failed to close escalate span", exc_info=True)
    return _esc_out


def run_retry_or_escalate(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> RetryResult | EscalateResult:
    """Executor syscall: auto-retry, escalating if retries exhausted."""
    import os

    from dgov.recovery import maybe_auto_retry

    session_root = os.path.abspath(session_root or project_root)
    result = maybe_auto_retry(session_root, slug, project_root)
    if result is None:
        return RetryResult(slug=slug, error="No retry/escalation action taken")
    if result.get("error"):
        return RetryResult(slug=slug, error=result["error"])
    if result.get("escalated"):
        return EscalateResult(
            slug=slug,
            new_slug=result.get("new_slug"),
            target_agent=result.get("to"),
        )
    return RetryResult(slug=slug, new_slug=result.get("new_slug"))


def run_complete_pane(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    reason: str = "auto_complete",
) -> StateTransitionResult:
    """Executor syscall: mark a pane as done (e.g., monitor auto-complete)."""
    import os

    from dgov.persistence import emit_event, settle_completion_state

    session_root = os.path.abspath(session_root or project_root)
    transition = settle_completion_state(session_root, slug, "done")
    if transition.changed:
        emit_event(session_root, "pane_done", slug, reason=reason)
    return StateTransitionResult(
        slug=slug,
        new_state="done",
        changed=transition.changed,
    )


def run_fail_pane(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    reason: str = "idle_timeout",
) -> StateTransitionResult:
    """Executor syscall: mark a pane as failed (e.g., monitor idle timeout)."""
    import os

    from dgov.persistence import emit_event, settle_completion_state

    session_root = os.path.abspath(session_root or project_root)
    transition = settle_completion_state(session_root, slug, "failed")
    if transition.changed:
        emit_event(session_root, "pane_failed", slug, reason=reason)
    return StateTransitionResult(
        slug=slug,
        new_state="failed",
        changed=transition.changed,
    )


def run_mark_reviewed(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    passed: bool,
) -> StateTransitionResult:
    """Executor syscall: transition pane to reviewed_pass or reviewed_fail."""
    import os

    from dgov.persistence import update_pane_state

    session_root = os.path.abspath(session_root or project_root)
    target = "reviewed_pass" if passed else "reviewed_fail"
    update_pane_state(session_root, slug, target, force=True)
    return StateTransitionResult(slug=slug, new_state=target, changed=True)


# ---------------------------------------------------------------------------
# DagKernel runtime adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DagRunResult:
    status: str
    merged: list[str]
    failed: list[str]
    skipped: list[str]
    run_id: int | None = None
    error: str | None = None


def run_dag_kernel(
    project_root: str,
    dag_definition: object,
    *,
    session_root: str | None = None,
    run_id: int = 0,
    auto_merge: bool = True,
    max_concurrent: int = 0,
    skip: frozenset[str] | None = None,
    poll_interval: float = 3.0,
    task_timeout: int = 600,
    progress: Callable[[str], None] | None = None,
) -> DagRunResult:
    """Drive a DAG through the DagKernel state machine.

    This is the runtime adapter: it translates kernel actions into executor
    syscalls and feeds events back. The kernel owns scheduling, readiness,
    merge ordering, and failure propagation. This function owns I/O.

    Args:
        project_root: Git repo root.
        dag_definition: A DagDefinition with .tasks and .project_root.
        session_root: Session root (defaults to project_root).
        run_id: DAG run ID for persistence.
        auto_merge: Whether to auto-merge reviewed_pass tasks.
        max_concurrent: Max concurrent workers (0=unlimited).
        poll_interval: Seconds between completion polls.
        task_timeout: Per-task wait timeout in seconds.
        progress: Optional callback for progress messages.
    """
    import os

    from dgov.kernel import (
        CloseTask,
        DagDone,
        DagKernel,
        DispatchTask,
        MergeTask,
        ReviewTask,
        SkipTask,
        TaskClosed,
        TaskDispatched,
        WaitForAny,
    )

    session_root = os.path.abspath(session_root or project_root)
    dag = dag_definition

    def _progress(msg: str) -> None:
        if progress:
            progress(msg)

    # Build dependency map from DagDefinition
    deps: dict[str, tuple[str, ...]] = {}
    task_timeouts: dict[str, int] = {}
    review_agents: dict[str, str] = {}
    for slug, task in dag.tasks.items():
        deps[slug] = tuple(task.depends_on)
        task_timeouts[slug] = getattr(task, "timeout_s", task_timeout)
        ra = getattr(task, "review_agent", "")
        if ra:
            review_agents[slug] = ra

    kernel = DagKernel(
        deps=deps,
        auto_merge=auto_merge,
        max_concurrent=max_concurrent,
        skip=skip or frozenset(),
        review_agents=review_agents,
    )
    actions = kernel.start()

    # Pane slug tracking (task_slug → pane_slug)
    pane_map: dict[str, str] = {}

    # Stable state tracking per pane for poll_once
    stable_states: dict[str, dict] = {}

    # Action queue — process non-blocking actions immediately,
    # only block on WaitForAny.
    queue: list = list(actions)

    def _extend_queue(new_actions: list) -> None:
        # If new actions contain a WaitForAny, drop any existing ones
        # from the queue — the new one has the latest waiting set.
        has_new_wait = any(isinstance(a, WaitForAny) for a in new_actions)
        if has_new_wait:
            queue[:] = [a for a in queue if not isinstance(a, WaitForAny)]
        queue.extend(new_actions)

    while queue:
        action = queue.pop(0)

        if isinstance(action, DagDone):
            _progress(
                f"DAG {action.status}: "
                f"{len(action.merged)} merged, "
                f"{len(action.failed)} failed, "
                f"{len(action.skipped)} skipped"
            )
            return DagRunResult(
                status=action.status,
                merged=list(action.merged),
                failed=list(action.failed),
                skipped=list(action.skipped),
                run_id=run_id,
            )

        if isinstance(action, DispatchTask):
            event = _dag_dispatch(dag, action.task_slug, run_id, session_root, _progress)
            if isinstance(event, TaskDispatched):
                pane_map[action.task_slug] = event.pane_slug
            _extend_queue(kernel.handle(event))
            continue

        if isinstance(action, WaitForAny):
            event = _dag_wait_any(
                project_root,
                session_root,
                action.task_slugs,
                pane_map,
                stable_states,
                task_timeouts,
                poll_interval,
            )
            _extend_queue(kernel.handle(event))
            continue

        if isinstance(action, ReviewTask):
            event = _dag_review(
                project_root,
                session_root,
                action.task_slug,
                action.pane_slug,
                _progress,
                review_agent=action.review_agent,
            )
            _extend_queue(kernel.handle(event))
            continue

        if isinstance(action, MergeTask):
            event = _dag_merge(
                project_root,
                session_root,
                action.task_slug,
                action.pane_slug,
                _progress,
            )
            _extend_queue(kernel.handle(event))
            continue

        if isinstance(action, SkipTask):
            _dag_skip(
                session_root,
                run_id,
                action.task_slug,
                dag,
                action.reason,
                _progress,
            )
            _extend_queue(kernel.handle(TaskClosed(action.task_slug)))
            continue

        if isinstance(action, CloseTask):
            _dag_close(
                project_root,
                session_root,
                action.task_slug,
                action.pane_slug,
                action.reason,
                _progress,
            )
            _extend_queue(kernel.handle(TaskClosed(action.task_slug)))
            continue

    # Queue exhausted without DagDone — shouldn't happen
    from dgov.kernel import DagTaskState

    merged = [s for s, st in kernel.task_states.items() if st == DagTaskState.MERGED]
    failed = [s for s, st in kernel.task_states.items() if st == DagTaskState.FAILED]
    skipped = [s for s, st in kernel.task_states.items() if st == DagTaskState.SKIPPED]
    return DagRunResult(
        status="partial" if merged else "failed",
        merged=merged,
        failed=failed,
        skipped=skipped,
        run_id=run_id,
        error="Kernel queue exhausted without DagDone",
    )


def _dag_dispatch(
    dag: object,
    task_slug: str,
    run_id: int,
    session_root: str,
    progress: Callable[[str], None],
) -> object:
    """Execute a DispatchTask action. Returns TaskDispatched or TaskDispatchFailed."""
    from dgov.kernel import TaskDispatched, TaskDispatchFailed
    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event, upsert_dag_task

    task = dag.tasks[task_slug]
    touches = [*task.files.create, *task.files.edit, *task.files.delete]
    packet = build_context_packet(
        task.prompt,
        file_claims=touches,
        commit_message=task.commit_message,
    )

    # Resolve logical agent name (e.g. qwen-35b) to physical backend
    # for preflight. create_worker_pane resolves again internally.
    from dgov.router import is_routable
    from dgov.router import resolve_agent as _resolve

    preflight_agent = task.agent
    if is_routable(task.agent):
        try:
            preflight_agent, _ = _resolve(task.agent, session_root, dag.project_root)
        except RuntimeError:
            preflight_agent = "pi"

    try:
        report = run_dispatch_preflight(
            dag.project_root,
            preflight_agent,
            session_root=session_root,
            packet=packet,
        )
        if not report.passed:
            failed_checks = [c.message for c in report.checks if not c.passed and c.critical]
            raise RuntimeError(f"Preflight failed: {'; '.join(failed_checks)}")

        dag_slug = f"r{run_id}-{task.slug}" if run_id else task.slug
        pane = create_worker_pane(
            project_root=dag.project_root,
            prompt=task.prompt,
            agent=task.agent,
            permission_mode=task.permission_mode,
            slug=dag_slug,
            session_root=session_root,
            context_packet=packet,
        )
        pane_slug = pane.slug
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "dispatched",
            task.agent,
            pane_slug=pane_slug,
        )
        emit_event(
            session_root,
            "dag_task_dispatched",
            f"dag/{run_id}",
            task=task_slug,
            pane_slug=pane_slug,
        )
        progress(f"  dispatched {task_slug} ({task.agent})")
        return TaskDispatched(task_slug, pane_slug)

    except Exception as exc:
        logger.error("Dispatch failed for %s: %s", task_slug, exc)
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "failed",
            task.agent,
            error=str(exc),
        )
        return TaskDispatchFailed(task_slug, str(exc))


def _dag_wait_any(
    project_root: str,
    session_root: str,
    task_slugs: tuple[str, ...],
    pane_map: dict[str, str],
    stable_states: dict[str, dict],
    task_timeouts: dict[str, int],
    poll_interval: float,
) -> object:
    """Poll active panes round-robin until one completes. Returns TaskWaitDone.

    Uses the unified WorkerObservation to check completion — same model
    the monitor uses for classification.
    """
    import time

    from dgov.kernel import TaskWaitDone, WorkerPhase
    from dgov.monitor import observe_worker

    start = time.monotonic()
    max_timeout = max(task_timeouts.get(s, 600) for s in task_slugs)

    while True:
        for task_slug in task_slugs:
            pane_slug = pane_map.get(task_slug, "")
            if not pane_slug:
                continue

            obs = observe_worker(project_root, session_root, pane_slug)
            if obs.phase in (WorkerPhase.DONE, WorkerPhase.FAILED, WorkerPhase.UNKNOWN):
                pane_state = "done" if obs.phase == WorkerPhase.DONE else "failed"
                return TaskWaitDone(task_slug, pane_slug, pane_state)

        elapsed = time.monotonic() - start
        if elapsed > max_timeout:
            return TaskWaitDone(task_slugs[0], pane_map.get(task_slugs[0], ""), "timed_out")

        from dgov.persistence import _wait_for_notify

        _wait_for_notify(session_root, poll_interval)


def _dag_review(
    project_root: str,
    session_root: str,
    task_slug: str,
    pane_slug: str,
    progress: Callable[[str], None],
    review_agent: str = "",
) -> object:
    """Execute a ReviewTask action. Returns TaskReviewDone."""
    from dgov.kernel import TaskReviewDone

    if review_agent:
        progress(f"  reviewing {task_slug} with {review_agent}")

    result = run_review_only(
        project_root,
        pane_slug,
        session_root=session_root,
        require_safe=True,
        require_commits=True,
        review_agent=review_agent,
    )
    progress(f"  reviewed {task_slug}: {result.verdict}")
    return TaskReviewDone(
        task_slug,
        passed=result.passed,
        verdict=result.verdict,
        commit_count=result.commit_count,
    )


def _dag_merge(
    project_root: str,
    session_root: str,
    task_slug: str,
    pane_slug: str,
    progress: Callable[[str], None],
) -> object:
    """Execute a MergeTask action. Returns TaskMergeDone."""
    from dgov.kernel import (
        TaskMergeDone,
        build_manifest_on_completion,
        validate_manifest_freshness,
    )
    from dgov.persistence import get_pane

    # Re-check staleness at merge time: review may have passed but main could
    # have advanced in the window between review and merge.
    pane = get_pane(session_root, pane_slug)
    if pane:
        base_sha = pane.get("base_sha", "")
        file_claims = tuple(pane.get("file_claims", ()) or ())
        wt = pane.get("worktree_path", "")
        manifest_root = wt if wt else project_root
        manifest = build_manifest_on_completion(
            manifest_root, pane_slug, base_sha, file_claims=file_claims
        )
        is_fresh, stale_files = validate_manifest_freshness(project_root, manifest)
        if not is_fresh:
            logger.warning(
                "Stale dependency for DAG %s: main changed %s (will attempt merge)",
                pane_slug,
                stale_files,
            )

    result = run_merge_only(project_root, pane_slug, session_root=session_root)
    if result.error:
        progress(f"  merge failed {task_slug}: {result.error}")
    else:
        progress(f"  merged {task_slug}")
    return TaskMergeDone(task_slug, error=result.error)


def _dag_skip(
    session_root: str,
    run_id: int,
    task_slug: str,
    dag: object,
    reason: str,
    progress: Callable[[str], None],
) -> None:
    """Persist a SkipTask action."""
    from dgov.persistence import upsert_dag_task

    task = dag.tasks[task_slug]
    upsert_dag_task(session_root, run_id, task_slug, "skipped", task.agent)
    progress(f"  skipped {task_slug}: {reason}")


def _dag_close(
    project_root: str,
    session_root: str,
    task_slug: str,
    pane_slug: str,
    reason: str,
    progress: Callable[[str], None],
) -> None:
    """Execute a CloseTask action."""
    if pane_slug:
        run_close_only(project_root, pane_slug, session_root=session_root, force=True)
    progress(f"  closed {task_slug}: {reason}")
