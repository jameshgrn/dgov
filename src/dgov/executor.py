"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dgov.context_packet import ContextPacket, build_context_packet
from dgov.decision import DecisionRecord, ReviewOutputDecision, ReviewOutputRequest, ReviewVerdict
from dgov.inspection import (
    ReviewInfo,
)
from dgov.merger import MergeError, MergeSuccess
from dgov.persistence import PaneState

if TYPE_CHECKING:
    from dgov.dag_parser import DagDefinition
    from dgov.kernel import (
        DagAction,
        DagEvent,
        TaskDispatched,
        TaskDispatchFailed,
        TaskMergeDone,
        TaskRetryStarted,
        TaskReviewDone,
        TaskWaitDone,
    )
    from dgov.merger import PaneMergeResult
    from dgov.persistence import WorkerPane

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewGate:
    review: ReviewInfo
    passed: bool
    verdict: str
    commit_count: int
    error: str | None = None


@dataclass(frozen=True)
class CleanupOnlyResult:
    slug: str
    action: str
    reason: str
    closed: bool = False
    force: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _PostDispatchWaitFailed:
    cleanup: CleanupOnlyResult
    error: str
    failure_stage: str


@dataclass(frozen=True)
class _PostDispatchReviewFailed:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    error: str = ""
    failure_stage: str = "review"


@dataclass(frozen=True)
class _PostDispatchMergeFailed:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    merge_result: PaneMergeResult | None = None
    error: str = ""
    failure_stage: str = "merge"


@dataclass(frozen=True)
class _PostDispatchReviewPending:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None


@dataclass(frozen=True)
class _PostDispatchReviewedPass:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None


@dataclass(frozen=True)
class _PostDispatchCompletedNoMerge:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None


@dataclass(frozen=True)
class _PostDispatchCompletedMerged:
    cleanup: CleanupOnlyResult
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision] | None = None
    merge_result: PaneMergeResult | None = None


PostDispatchOutcome = (
    _PostDispatchWaitFailed
    | _PostDispatchReviewFailed
    | _PostDispatchMergeFailed
    | _PostDispatchReviewPending
    | _PostDispatchReviewedPass
    | _PostDispatchCompletedNoMerge
    | _PostDispatchCompletedMerged
)


@dataclass(frozen=True)
class PostDispatchResult:
    slug: str
    outcome: PostDispatchOutcome

    @property
    def state(self) -> str:
        if isinstance(
            self.outcome,
            (_PostDispatchWaitFailed, _PostDispatchReviewFailed, _PostDispatchMergeFailed),
        ):
            return PaneState.FAILED
        if isinstance(self.outcome, _PostDispatchReviewPending):
            return "review_pending"
        if isinstance(self.outcome, _PostDispatchReviewedPass):
            return PaneState.REVIEWED_PASS
        return "completed"

    @property
    def review(self) -> ReviewInfo | None:
        if isinstance(self.outcome, _PostDispatchWaitFailed):
            return None
        return self.outcome.review

    @property
    def review_record(self) -> DecisionRecord[ReviewOutputDecision] | None:
        if isinstance(self.outcome, _PostDispatchWaitFailed):
            return None
        return self.outcome.review_record

    @property
    def merge_result(self) -> PaneMergeResult | None:
        if isinstance(self.outcome, (_PostDispatchMergeFailed, _PostDispatchCompletedMerged)):
            return self.outcome.merge_result
        return None

    @property
    def cleanup(self) -> CleanupOnlyResult:
        return self.outcome.cleanup

    @property
    def error(self) -> str | None:
        if isinstance(
            self.outcome,
            (_PostDispatchWaitFailed, _PostDispatchReviewFailed, _PostDispatchMergeFailed),
        ):
            return self.outcome.error
        return None

    @property
    def failure_stage(self) -> str | None:
        if isinstance(
            self.outcome,
            (_PostDispatchWaitFailed, _PostDispatchReviewFailed, _PostDispatchMergeFailed),
        ):
            return self.outcome.failure_stage
        return None


@dataclass(frozen=True)
class ReviewOnlyResult:
    slug: str
    review: ReviewInfo
    passed: bool
    review_record: DecisionRecord[ReviewOutputDecision]
    error: str | None = None

    @property
    def verdict(self) -> str:
        return self.review.verdict

    @property
    def commit_count(self) -> int:
        return self.review.commit_count


@dataclass(frozen=True)
class _WaitCompleted:
    wait_result: dict | None = None
    pane_state: str | PaneState | None = None


@dataclass(frozen=True)
class _WaitFailed:
    error: str
    failure_stage: str
    suggest_escalate: bool
    wait_result: dict | None = None
    pane_state: str | PaneState | None = None


WaitOutcome = _WaitCompleted | _WaitFailed


@dataclass(frozen=True)
class WaitOnlyResult:
    slug: str
    outcome: WaitOutcome

    @classmethod
    def completed(
        cls, slug: str, *, wait_result: dict | None = None, pane_state: str | PaneState | None = None
    ) -> WaitOnlyResult:
        return cls(
            slug=slug, outcome=_WaitCompleted(wait_result=wait_result, pane_state=pane_state)
        )

    @classmethod
    def failed(
        cls,
        slug: str,
        *,
        error: str,
        failure_stage: str,
        suggest_escalate: bool,
        wait_result: dict | None = None,
        pane_state: str | PaneState | None = None,
    ) -> WaitOnlyResult:
        return cls(
            slug=slug,
            outcome=_WaitFailed(
                error=error,
                failure_stage=failure_stage,
                suggest_escalate=suggest_escalate,
                wait_result=wait_result,
                pane_state=pane_state,
            ),
        )

    @property
    def state(self) -> str:
        return PaneState.FAILED if isinstance(self.outcome, _WaitFailed) else "completed"

    @property
    def wait_result(self) -> dict | None:
        return self.outcome.wait_result

    @property
    def pane_state(self) -> str | PaneState | None:
        return self.outcome.pane_state

    @property
    def error(self) -> str | None:
        if isinstance(self.outcome, _WaitFailed):
            return self.outcome.error
        return None

    @property
    def failure_stage(self) -> str | None:
        if isinstance(self.outcome, _WaitFailed):
            return self.outcome.failure_stage
        return None

    @property
    def suggest_escalate(self) -> bool:
        if isinstance(self.outcome, _WaitFailed):
            return self.outcome.suggest_escalate
        return False


@dataclass(frozen=True)
class MergeOnlyResult:
    slug: str
    merge_result: PaneMergeResult

    @property
    def error(self) -> str | None:
        return _merge_result_error(self.merge_result)


@dataclass(frozen=True)
class _ReviewMergeReviewError:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    error: str
    failure_stage: str = "review_error"


@dataclass(frozen=True)
class _ReviewMergeReviewFailed:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    error: str
    failure_stage: str = "review_failed"


@dataclass(frozen=True)
class _ReviewMergeNoCommits:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]


@dataclass(frozen=True)
class _ReviewMergeFailed:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    merge_result: PaneMergeResult
    error: str
    failure_stage: str = "merge_failed"


@dataclass(frozen=True)
class _ReviewMergeMerged:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    merge_result: PaneMergeResult


ReviewMergeOutcome = (
    _ReviewMergeReviewError
    | _ReviewMergeReviewFailed
    | _ReviewMergeNoCommits
    | _ReviewMergeFailed
    | _ReviewMergeMerged
)


@dataclass(frozen=True)
class ReviewMergeResult:
    slug: str
    outcome: ReviewMergeOutcome

    @property
    def review(self) -> ReviewInfo:
        return self.outcome.review

    @property
    def review_record(self) -> DecisionRecord[ReviewOutputDecision]:
        return self.outcome.review_record

    @property
    def merge_result(self) -> PaneMergeResult | None:
        if isinstance(self.outcome, (_ReviewMergeFailed, _ReviewMergeMerged)):
            return self.outcome.merge_result
        return None

    @property
    def failure_stage(self) -> str | None:
        if isinstance(
            self.outcome, (_ReviewMergeReviewError, _ReviewMergeReviewFailed, _ReviewMergeFailed)
        ):
            return self.outcome.failure_stage
        return None

    @property
    def error(self) -> str | None:
        if isinstance(
            self.outcome, (_ReviewMergeReviewError, _ReviewMergeReviewFailed, _ReviewMergeFailed)
        ):
            return self.outcome.error
        return None


@dataclass(frozen=True)
class _LandMissingPane:
    error: str
    failure_stage: str = "land"


@dataclass(frozen=True)
class _LandFailed:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    error: str
    failure_stage: str
    merge_result: PaneMergeResult | None = None


@dataclass(frozen=True)
class _LandNoMerge:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    cleanup: CleanupOnlyResult


@dataclass(frozen=True)
class _LandMerged:
    review: ReviewInfo
    review_record: DecisionRecord[ReviewOutputDecision]
    merge_result: PaneMergeResult
    cleanup: CleanupOnlyResult


LandOutcome = _LandMissingPane | _LandFailed | _LandNoMerge | _LandMerged


@dataclass(frozen=True)
class LandResult:
    slug: str
    outcome: LandOutcome

    @property
    def review(self) -> ReviewInfo | None:
        if isinstance(self.outcome, (_LandFailed, _LandNoMerge, _LandMerged)):
            return self.outcome.review
        return None

    @property
    def review_record(self) -> DecisionRecord[ReviewOutputDecision] | None:
        if isinstance(self.outcome, (_LandFailed, _LandNoMerge, _LandMerged)):
            return self.outcome.review_record
        return None

    @property
    def merge_result(self) -> PaneMergeResult | None:
        if isinstance(self.outcome, (_LandFailed, _LandMerged)):
            return self.outcome.merge_result
        return None

    @property
    def cleanup(self) -> CleanupOnlyResult | None:
        if isinstance(self.outcome, (_LandNoMerge, _LandMerged)):
            return self.outcome.cleanup
        return None

    @property
    def failure_stage(self) -> str | None:
        if isinstance(self.outcome, (_LandMissingPane, _LandFailed)):
            return self.outcome.failure_stage
        return None

    @property
    def error(self) -> str | None:
        if isinstance(self.outcome, (_LandMissingPane, _LandFailed)):
            return self.outcome.error
        return None


@dataclass
class PaneFinalizeResult:
    slug: str
    review: ReviewInfo
    merge_result: PaneMergeResult | None
    error: str | None
    cleanup_error: str | None


def _merge_result_error(result: PaneMergeResult) -> str | None:
    if isinstance(result, MergeError):
        return result.error
    if hasattr(result, "conflicts"):
        hint = getattr(result, "hint", None)
        conflicts = ", ".join(getattr(result, "conflicts", []))
        return hint or f"Merge conflicts: {conflicts}" if conflicts else "Merge conflicts"
    return None


def run_dispatch_only(
    project_root: str,
    prompt: str,
    agent: str,
    *,
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
    slug: str | None = None,
    env_vars: dict[str, str] | None = None,
    extra_flags: str | None = None,
    existing_worktree: str | None = None,
    skip_auto_structure: bool = False,
    role: str = "worker",
    parent_slug: str | None = None,
    context_packet: ContextPacket | None = None,
) -> WorkerPane:
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


# Policy lookup for cleanup decisions based on (state, failure_stage).
# Each entry maps to a dict of parameters for close_worker_pane.
# Special entries can override the default close path with early returns.
_CLEANUP_POLICY: dict[tuple[str, str | None], dict] = {
    (PaneState.FAILED, "timeout"): {"force": False},
    (PaneState.FAILED, "recovery"): {"force": False},
    (PaneState.FAILED, "review"): {"force": False},
    (PaneState.FAILED, "worker_failed"): {"force": True},
    (PaneState.CLOSED, None): {
        "force": True,
        "action": "closed",
        "reason": "completed_no_commits",
    },
}


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

    policy = _CLEANUP_POLICY.get((state, failure_stage))
    if policy is not None:
        if policy.get("action") == "closed":
            # 0-commit completed panes: close cleanly (captures transcripts)
            session_root = session_root or project_root
            close_worker_pane(project_root, slug, session_root, force=policy["force"])
            return CleanupOnlyResult(
                slug=slug,
                action=policy["action"],
                reason=policy["reason"],
            )
        force = policy.get("force", False)
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


def _should_suggest_escalate(session_root: str, project_root: str, slug: str) -> bool:
    """Check if a stronger agent is available for escalation."""
    from dgov.persistence import get_pane
    from dgov.recovery import _resolve_escalation_target

    try:
        rec = get_pane(session_root, slug)
    except Exception:  # noqa: BLE001
        return False
    if not rec:
        return False
    agent = rec.get("agent", "")
    return bool(agent) and _resolve_escalation_target(agent, project_root) != agent


def _read_exit_message(session_root: str, slug: str) -> str:
    """Read exit code from .exit file for a human-readable failure message."""
    from dgov.persistence import STATE_DIR

    try:
        exit_path = Path(session_root) / STATE_DIR / "done" / (slug + ".exit")
        if exit_path.exists():
            code = exit_path.read_text().strip()
            return f"Worker exited with code {code}"
    except Exception:
        logger.debug("failed to read exit code for %s", slug, exc_info=True)
    return "Worker exited with an error"


def _handle_timeout_recovery(
    project_root: str,
    session_root: str,
    current_slug: str,
    *,
    escalate_to: str | None,
    retry_agent: str | None,
    permission_mode: str,
) -> tuple[str | None, str | None]:
    """Handle recovery after PaneTimeoutError. Returns (new_slug, error)."""
    from dgov.recovery import escalate_worker_pane, retry_worker_pane

    if escalate_to:
        esc_result = escalate_worker_pane(
            project_root,
            current_slug,
            target_agent=escalate_to,
            session_root=session_root,
            permission_mode=permission_mode,
        )
        if esc_result.get("error"):
            return None, f"Escalation failed: {esc_result['error']}"
        return esc_result["new_slug"], None

    retry_result = retry_worker_pane(
        project_root,
        current_slug,
        session_root=session_root,
        agent=retry_agent,
    )
    if retry_result.get("error"):
        return None, f"Retry failed: {retry_result['error']}"
    return retry_result["new_slug"], None


def _handle_failed_pane(
    project_root: str,
    session_root: str,
    current_slug: str,
    *,
    auto_retry: bool,
    retries_left: int,
    timeout: int,
    poll: int,
    stable: int,
) -> tuple[str, dict | None, str | None, int]:
    """Handle a pane that finished in 'failed' state.

    Returns (slug, wait_result, pane_state, retries_left).
    """
    from dgov.persistence import get_pane
    from dgov.waiter import PaneTimeoutError, wait_worker_pane

    if not (auto_retry and retries_left > 0):
        pane = get_pane(session_root, current_slug)
        return current_slug, None, pane.get("state") if pane else PaneState.FAILED, retries_left

    from dgov.recovery import maybe_auto_retry

    retry_result = maybe_auto_retry(session_root, current_slug, project_root)
    if not retry_result or not retry_result.get("new_slug"):
        pane = get_pane(session_root, current_slug)
        return current_slug, None, pane.get("state") if pane else PaneState.FAILED, retries_left

    new_slug = retry_result["new_slug"]
    retries_left -= 1
    try:
        wait_result = wait_worker_pane(
            project_root,
            new_slug,
            session_root=session_root,
            timeout=timeout,
            poll=poll,
            stable=stable,
            auto_retry=False,
        )
    except PaneTimeoutError:
        return new_slug, None, PaneState.TIMED_OUT, retries_left

    pane = get_pane(session_root, new_slug)
    return new_slug, wait_result, pane.get("state") if pane else None, retries_left


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

    current_slug: str = slug
    retries_left = max_retries
    wait_result: dict | None = None

    # --- Wait loop with timeout recovery ---
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
                return WaitOnlyResult.failed(
                    slug=current_slug,
                    error=f"Worker timed out after {timeout}s (retries exhausted)",
                    failure_stage="timeout",
                    suggest_escalate=_should_suggest_escalate(
                        session_root, project_root, current_slug
                    ),
                )

            new_slug, error = _handle_timeout_recovery(
                project_root,
                session_root,
                current_slug,
                escalate_to=escalate_to,
                retry_agent=retry_agent,
                permission_mode=permission_mode,
            )
            if error:
                _phase("failed", current_slug)
                return WaitOnlyResult.failed(
                    slug=current_slug,
                    error=error,
                    failure_stage="recovery",
                    suggest_escalate=_should_suggest_escalate(
                        session_root, project_root, current_slug
                    ),
                )
            assert new_slug is not None
            current_slug = new_slug
            retries_left -= 1
            _phase("waiting", current_slug)

    # --- Post-wait state evaluation ---
    pane = get_pane(session_root, current_slug)
    pane_state = pane.get("state") if pane else None

    if pane_state == PaneState.FAILED:
        current_slug, wait_result, pane_state, retries_left = _handle_failed_pane(
            project_root,
            session_root,
            current_slug,
            auto_retry=auto_retry,
            retries_left=retries_left,
            timeout=timeout,
            poll=poll,
            stable=stable,
        )
        if pane_state == PaneState.FAILED:
            _phase("failed", current_slug)
            exit_msg = _read_exit_message(session_root, current_slug)
            return WaitOnlyResult.failed(
                slug=current_slug,
                wait_result=wait_result,
                pane_state=pane_state,
                error=f"{exit_msg} (check logs with: dgov pane output {current_slug})",
                failure_stage="worker_failed",
                suggest_escalate=_should_suggest_escalate(
                    session_root, project_root, current_slug
                ),
            )

    if pane_state in (PaneState.TIMED_OUT, PaneState.ABANDONED):
        _phase("failed", current_slug)
        return WaitOnlyResult.failed(
            slug=current_slug,
            wait_result=wait_result,
            pane_state=pane_state,
            error=f"Worker ended in {pane_state} state",
            failure_stage="worker_failed",
            suggest_escalate=_should_suggest_escalate(session_root, project_root, current_slug),
        )

    # --- Success ---
    _wait_result = WaitOnlyResult.completed(
        slug=current_slug,
        wait_result=wait_result,
        pane_state=pane_state,
    )
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

    This sequentially invokes the underlying executor policies.
    """
    import os

    from dgov.persistence import set_pane_metadata

    session_root = os.path.abspath(session_root or project_root)
    claimed_slugs: set[str] = {slug}

    try:
        set_pane_metadata(session_root, slug, landing=True)
    except Exception:
        logger.debug("failed to set landing flag for %s", slug, exc_info=True)

    def _phase(name: str, p_slug: str) -> None:
        if phase_callback is not None:
            phase_callback(name, p_slug)

    wait_res = None
    review_res = None
    merge_res = None
    cleanup_res = None
    current_slug = slug
    state = PaneState.FAILED
    failure_stage = None
    error = None

    try:
        # Wait phase
        wait_res = run_wait_only(
            project_root,
            current_slug,
            session_root=session_root,
            timeout=timeout,
            max_retries=max_retries,
            permission_mode=permission_mode,
            retry_agent=retry_agent,
            escalate_to=escalate_to,
            phase_callback=phase_callback,
        )
        current_slug = wait_res.slug
        if current_slug not in claimed_slugs:
            try:
                set_pane_metadata(session_root, current_slug, landing=True)
            except Exception:
                logger.debug("failed to set landing flag for %s", current_slug, exc_info=True)
            claimed_slugs.add(current_slug)
        if wait_res.state != "completed":
            state = PaneState.FAILED
            failure_stage = wait_res.failure_stage
            error = wait_res.error
            _phase("failed", current_slug)
        else:
            # Review phase
            _phase("reviewing", current_slug)
            review_res = run_review_only(
                project_root,
                current_slug,
                session_root=session_root,
                require_safe=False,
                require_commits=True,
            )
            if review_res.error is not None:
                state = PaneState.FAILED
                failure_stage = "review"
                error = f"Review failed: {review_res.error}"
                _phase("failed", current_slug)
            elif review_res.verdict != ReviewVerdict.SAFE:
                state = "review_pending"
            elif review_res.commit_count == 0:
                state = "completed"
                _phase("completed", current_slug)
            elif not auto_merge:
                state = PaneState.REVIEWED_PASS
            else:
                # Merge phase
                _phase("merging", current_slug)
                merge_res = run_merge_only(
                    project_root,
                    current_slug,
                    session_root=session_root,
                    resolve=resolve,
                    squash=squash,
                    rebase=rebase,
                )
                if merge_res.error is not None:
                    state = PaneState.FAILED
                    failure_stage = "merge"
                    error = f"Merge failed: {merge_res.error}"
                    _phase("failed", current_slug)
                else:
                    state = "completed"
                    _phase("completed", current_slug)

    finally:
        if state == "completed":
            cleanup_state = "completed"
            if review_res and review_res.commit_count == 0:
                cleanup_state = PaneState.CLOSED
        elif state == PaneState.FAILED:
            cleanup_state = PaneState.FAILED
        elif state in (PaneState.REVIEWED_PASS, "review_pending"):
            cleanup_state = "review_pending"
        else:
            cleanup_state = state

        cleanup_res = run_cleanup_only(
            project_root,
            current_slug,
            session_root=session_root,
            state=cleanup_state,
            failure_stage=failure_stage,
        )
        current_slug = cleanup_res.slug if cleanup_res else current_slug

        for claimed in claimed_slugs:
            try:
                set_pane_metadata(session_root, claimed, landing=False)
            except Exception:
                logger.debug("failed to unset landing flag for %s", claimed, exc_info=True)

    if state == PaneState.FAILED:
        if review_res is None:
            outcome: PostDispatchOutcome = _PostDispatchWaitFailed(
                cleanup=cleanup_res,
                error=error or "Unknown failure",
                failure_stage=failure_stage or "wait",
            )
        elif merge_res is not None and merge_res.merge_result is not None:
            outcome = _PostDispatchMergeFailed(
                cleanup=cleanup_res,
                review=review_res.review,
                review_record=review_res.review_record,
                merge_result=merge_res.merge_result,
                error=error or "Unknown merge failure",
            )
        else:
            outcome = _PostDispatchReviewFailed(
                cleanup=cleanup_res,
                review=review_res.review,
                review_record=review_res.review_record,
                error=error or "Unknown review failure",
            )
        return PostDispatchResult(slug=current_slug, outcome=outcome)
    if state == "review_pending":
        assert review_res is not None
        return PostDispatchResult(
            slug=current_slug,
            outcome=_PostDispatchReviewPending(
                cleanup=cleanup_res,
                review=review_res.review,
                review_record=review_res.review_record,
            ),
        )
    if state == PaneState.REVIEWED_PASS:
        assert review_res is not None
        return PostDispatchResult(
            slug=current_slug,
            outcome=_PostDispatchReviewedPass(
                cleanup=cleanup_res,
                review=review_res.review,
                review_record=review_res.review_record,
            ),
        )
    if merge_res is not None and merge_res.merge_result is not None:
        assert review_res is not None
        return PostDispatchResult(
            slug=current_slug,
            outcome=_PostDispatchCompletedMerged(
                cleanup=cleanup_res,
                review=review_res.review,
                review_record=review_res.review_record,
                merge_result=merge_res.merge_result,
            ),
        )
    assert review_res is not None
    return PostDispatchResult(
        slug=current_slug,
        outcome=_PostDispatchCompletedNoMerge(
            cleanup=cleanup_res,
            review=review_res.review,
            review_record=review_res.review_record,
        ),
    )


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

    # Explicit claims: either touches param was provided or packet has file_claims
    explicit_claims = touches is not None or bool(packet.file_claims)

    return run_preflight(
        project_root=project_root,
        agent=agent,
        touches=list(packet.touches),
        expected_branch=expected_branch,
        session_root=session_root,
        skip_deps=skip_deps,
        derived_only=not explicit_claims,
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


def _run_model_review(
    request: ReviewOutputRequest,
    record: DecisionRecord[ReviewOutputDecision],
    slug: str,
) -> DecisionRecord[ReviewOutputDecision]:
    """Run model-backed review if deterministic passed and review_agent is set.

    Returns the model record if it flags concerns, otherwise the original.
    """
    from dgov.decision import ProviderError

    try:
        from dgov.decision_providers import ModelReviewProvider

        model_provider = ModelReviewProvider()
        model_record = model_provider.review_output(request)
        if model_record.decision.verdict != ReviewVerdict.SAFE:
            logger.info(
                "Model review (%s) flagged concerns for %s: %s",
                request.review_agent,
                slug,
                model_record.decision.issues,
            )
            return model_record
    except ProviderError:
        logger.debug("Model review failed for %s, using deterministic result", slug)
    return record


def _build_review_info(record: DecisionRecord[ReviewOutputDecision], slug: str) -> ReviewInfo:
    """Construct ReviewInfo from a review DecisionRecord."""
    artifact = record.artifact if isinstance(record.artifact, ReviewInfo) else None
    if artifact:
        review = artifact
    else:
        review = ReviewInfo(
            slug=slug,
            verdict=record.decision.verdict,
            commit_count=record.decision.commit_count,
        )
    if record.decision.issues:
        review.issues = list(record.decision.issues)
    if record.decision.reason and not review.error:
        review.error = record.decision.reason
    return review


def _apply_review_policy(
    verdict: str,
    commit_count: int,
    error: str | None,
    *,
    require_safe: bool,
    require_commits: bool,
    session_root: str | None,
    slug: str,
) -> tuple[bool, str | None]:
    """Apply review pass/fail policy. Returns (passed, error)."""
    passed = error is None
    if passed and require_commits and commit_count == 0:
        _pane_state = None
        if session_root:
            try:
                from dgov.persistence import get_pane

                _p = get_pane(session_root, slug)
                _pane_state = _p.get("state") if _p else None
            except Exception:
                logger.debug("failed to get pane state for %s", slug, exc_info=True)
        if _pane_state not in (PaneState.DONE, PaneState.MERGED):
            passed = False
            error = "No commits to merge"
    if passed and require_safe and verdict != ReviewVerdict.SAFE:
        passed = False
        error = f"Review verdict is {verdict}; refusing to merge"
    return passed, error


def _validate_review_manifest(
    project_root: str, session_root: str, slug: str, review: ReviewInfo
) -> None:
    """Check claim violations and staleness, mutating review in place."""
    import os

    from dgov.gitops import build_manifest_on_completion, validate_manifest_freshness
    from dgov.persistence import get_pane

    sr = os.path.abspath(session_root)
    try:
        pane = get_pane(sr, slug)
    except (OSError, Exception):
        pane = None
    if not pane:
        return

    base_sha = pane.get("base_sha", "")
    file_claims = tuple(pane.get("file_claims", ()) or ())
    wt = pane.get("worktree_path", "")
    manifest_root = wt if wt else project_root
    manifest = build_manifest_on_completion(manifest_root, slug, base_sha, file_claims=file_claims)
    if manifest.claim_violations:
        review.contract.claim_violations = list(manifest.claim_violations)
        logger.info("Claim violations for %s: %s", slug, manifest.claim_violations)
    is_fresh, stale_files = validate_manifest_freshness(project_root, manifest)
    if not is_fresh:
        review.freshness_info.stale_files = stale_files
        review.freshness_info.status = "warn"
        logger.warning(
            "Stale dependency for %s: main changed %s since base (will attempt merge)",
            slug,
            stale_files,
        )


def _check_review_test_coverage(session_root: str, slug: str, review: ReviewInfo) -> None:
    """Check test coverage for changed files, mutating review in place."""
    from dgov.inspection import check_test_coverage

    changed = review.changed_files
    if changed:
        missing_tests = check_test_coverage(changed, session_root=session_root)
        if missing_tests:
            review.contract.missing_test_coverage = missing_tests
            logger.warning("Test coverage warning for %s: %s", slug, missing_tests)


def run_review_only(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    full: bool = False,
    emit_events: bool = True,
    require_safe: bool = True,
    require_commits: bool = True,
    review_agent: str | None = None,
    tests_pass: bool = True,
    lint_clean: bool = True,
    post_merge_check: str | None = None,
    evals: tuple[dict, ...] = (),
) -> ReviewOnlyResult:
    """Run the canonical review operation without merging."""
    _review_span_id = None
    try:
        from dgov.spans import SpanKind, open_span

        _review_span_id = open_span(session_root or "", slug, SpanKind.REVIEW)
    except Exception:
        logger.debug("failed to open review span for %s", slug, exc_info=True)

    from dgov.decision import DecisionKind

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
        emit_events=emit_events,
        agent_id=agent_id,
        review_agent=review_agent,
        tests_pass=tests_pass,
        lint_clean=lint_clean,
        post_merge_check=post_merge_check,
        evals=evals,
    )

    # Stage 1: Deterministic inspection (always runs, free)
    from dgov.provider_registry import get_provider

    provider = get_provider(DecisionKind.REVIEW_OUTPUT, session_root=session_root)
    record = provider.review_output(request)

    # Stage 2: Model review (only if deterministic passed AND review_agent is set)
    if (
        review_agent
        and record.decision.verdict == ReviewVerdict.SAFE
        and record.decision.commit_count > 0
    ):
        record = _run_model_review(request, record, slug)

    # Build ReviewInfo + apply policy
    review = _build_review_info(record, slug)
    verdict = record.decision.verdict
    commit_count = record.decision.commit_count
    passed, error = _apply_review_policy(
        verdict,
        commit_count,
        record.decision.reason,
        require_safe=require_safe,
        require_commits=require_commits,
        session_root=session_root,
        slug=slug,
    )

    # Post-review validation (manifest + test coverage)
    if passed and commit_count > 0 and session_root:
        _validate_review_manifest(project_root, session_root, slug, review)
        _check_review_test_coverage(session_root, slug, review)

    _review_result = ReviewOnlyResult(
        slug=slug,
        review=review,
        passed=passed,
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
                if review.tests.passed
                else (0 if review.tests.passed is False else -1),
                stale_files=json.dumps(review.freshness_info.stale_files),
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

    session_root = os.path.abspath(session_root or project_root)

    review_res = run_review_only(
        project_root,
        slug,
        session_root=session_root,
        require_safe=False,
        require_commits=True,
    )

    if review_res.error is not None:
        return ReviewMergeResult(
            slug=slug,
            outcome=_ReviewMergeReviewError(
                review=review_res.review,
                review_record=review_res.review_record,
                error=review_res.error,
            ),
        )

    if review_res.verdict != ReviewVerdict.SAFE:
        return ReviewMergeResult(
            slug=slug,
            outcome=_ReviewMergeReviewFailed(
                review=review_res.review,
                review_record=review_res.review_record,
                error=f"Review verdict is {review_res.verdict}; refusing to merge",
            ),
        )

    if review_res.commit_count == 0:
        return ReviewMergeResult(
            slug=slug,
            outcome=_ReviewMergeNoCommits(
                review=review_res.review,
                review_record=review_res.review_record,
            ),
        )

    merge_res = run_merge_only(
        project_root,
        slug,
        session_root=session_root,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
    )

    if merge_res.error is not None:
        return ReviewMergeResult(
            slug=slug,
            outcome=_ReviewMergeFailed(
                review=review_res.review,
                review_record=review_res.review_record,
                merge_result=merge_res.merge_result,
                error=merge_res.error,
            ),
        )

    return ReviewMergeResult(
        slug=slug,
        outcome=_ReviewMergeMerged(
            review=review_res.review,
            review_record=review_res.review_record,
            merge_result=merge_res.merge_result,
        ),
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
        return LandResult(
            slug=slug,
            outcome=_LandMissingPane(error=f"Pane not found: {slug}"),
        )

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
            outcome=_LandFailed(
                review=result.review,
                review_record=result.review_record,
                merge_result=result.merge_result,
                failure_stage=result.failure_stage or "land",
                error=result.error,
            ),
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
        outcome=_LandMerged(
            review=result.review,
            review_record=result.review_record,
            merge_result=result.merge_result,
            cleanup=cleanup,
        )
        if result.merge_result is not None
        else _LandNoMerge(
            review=result.review,
            review_record=result.review_record,
            cleanup=cleanup,
        ),
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
                    review=ReviewInfo(slug=slug),
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
                    review=lifecycle.review or ReviewInfo(slug=slug),
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

    _merge_error = _merge_result_error(merge_result) or ""
    _merge_out = MergeOnlyResult(slug=slug, merge_result=merge_result)

    if _merge_span_id is not None:
        try:
            from dgov.spans import SpanOutcome, close_span

            _mo = SpanOutcome.FAILURE if _merge_error else SpanOutcome.SUCCESS
            close_span(
                session_root or "",
                _merge_span_id,
                _mo,
                files_changed=merge_result.files_changed
                if isinstance(merge_result, MergeSuccess)
                else 0,
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
    allow_abandoned: bool = False,
) -> StateTransitionResult:
    """Executor syscall: mark a pane as done (e.g., monitor auto-complete)."""
    import os

    from dgov.persistence import emit_event, settle_completion_state

    session_root = os.path.abspath(session_root or project_root)
    transition = settle_completion_state(
        session_root, slug, PaneState.DONE, allow_abandoned=allow_abandoned
    )
    if transition.changed:
        emit_event(session_root, "pane_done", slug, reason=reason)
    return StateTransitionResult(
        slug=slug,
        new_state=PaneState.DONE,
        changed=transition.changed,
    )


def run_fail_pane(
    project_root: str,
    slug: str,
    *,
    session_root: str | None = None,
    reason: str = "idle_timeout",
    allow_abandoned: bool = False,
) -> StateTransitionResult:
    """Executor syscall: mark a pane as failed (e.g., monitor idle timeout)."""
    import os

    from dgov.persistence import emit_event, settle_completion_state

    session_root = os.path.abspath(session_root or project_root)
    transition = settle_completion_state(
        session_root, slug, PaneState.FAILED, allow_abandoned=allow_abandoned
    )
    if transition.changed:
        emit_event(session_root, "pane_failed", slug, reason=reason)
    return StateTransitionResult(
        slug=slug,
        new_state=PaneState.FAILED,
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

    from dgov.persistence import emit_event, update_pane_state

    session_root = os.path.abspath(session_root or project_root)
    target = PaneState.REVIEWED_PASS if passed else PaneState.REVIEWED_FAIL
    update_pane_state(session_root, slug, target, force=True)
    emit_event(session_root, f"pane_{target}", slug)
    return StateTransitionResult(slug=slug, new_state=target, changed=True)


def run_enqueue_merge(
    session_root: str,
    slug: str,
    requester: str = "governor",
) -> dict:
    """Executor syscall: enqueue a merge request."""
    from dgov.persistence import emit_event, enqueue_merge

    ticket = enqueue_merge(session_root, slug, requester)
    emit_event(session_root, "merge_enqueued", slug, ticket=ticket, requester=requester)
    return {"ticket": ticket, "slug": slug, "requester": requester}


def run_process_merge(
    project_root: str,
    session_root: str,
    *,
    resolve: str = "skip",
    squash: bool = True,
    rebase: bool = False,
) -> dict:
    """Executor syscall: claim and execute next pending merge."""
    from dgov.persistence import claim_next_merge, complete_merge, emit_event

    claimed = claim_next_merge(session_root)
    if not claimed:
        return {"status": "empty"}
    slug = claimed["branch"]
    ticket = claimed["ticket"]
    try:
        landed = run_land_only(
            project_root,
            slug,
            session_root=session_root,
            resolve=resolve,
            squash=squash,
            rebase=rebase,
        )
        if landed.merge_result:
            result = dataclasses.asdict(landed.merge_result)
        else:
            result = {"error": landed.error or "Review failed"}
        success = result.get("error") is None
        complete_merge(session_root, ticket, success, json.dumps(result))
        if success:
            emit_event(session_root, "merge_completed", slug, ticket=ticket, success=True)
        return {"ticket": ticket, "slug": slug, "result": result, "success": success}
    except Exception as exc:
        complete_merge(session_root, ticket, False, json.dumps({"error": str(exc)}))
        return {"ticket": ticket, "slug": slug, "error": str(exc), "success": False}


def run_resume_dag(session_root: str, run_id: int) -> None:
    """Executor syscall: mark a DAG run as resumed."""
    from dgov.persistence import emit_event, update_dag_run

    update_dag_run(session_root, run_id, status="resumed")
    emit_event(session_root, "dag_resumed", f"dag/{run_id}", dag_run_id=run_id)


def run_cancel_dag(session_root: str, run_id: int) -> dict:
    """Executor syscall: cancel an open DAG run and close its live panes."""
    from dgov.lifecycle import close_worker_pane
    from dgov.persistence import (
        emit_event,
        get_dag_run,
        get_pane,
        list_dag_tasks,
        update_dag_run,
        upsert_dag_task,
    )

    run = get_dag_run(session_root, run_id)
    if not run:
        return {"error": f"DAG run {run_id} not found"}

    status = str(run.get("status", ""))
    if status == "cancelled":
        return {"run_id": run_id, "status": "cancelled", "already_cancelled": True}
    if status in {"completed", "failed"}:
        return {"error": f"Run {run_id} is already terminal: {status}"}

    state_json = run.get("state_json", {})
    task_states = dict(state_json.get("task_states", {}))
    task_rows = list_dag_tasks(session_root, run_id)
    task_rows_by_slug = {row["slug"]: row for row in task_rows}

    closed: list[str] = []
    cancelled: list[str] = []
    pane_slugs = {str(row["pane_slug"]) for row in task_rows if row.get("pane_slug")}

    # Bug #184: Also close retry descendant panes
    from dgov.persistence import get_child_panes

    all_pane_slugs = set(pane_slugs)
    to_check = list(pane_slugs)
    while to_check:
        parent = to_check.pop()
        for child in get_child_panes(session_root, parent):
            child_slug = str(child.get("slug", ""))
            if child_slug and child_slug not in all_pane_slugs:
                all_pane_slugs.add(child_slug)
                to_check.append(child_slug)

    for pane_slug in sorted(all_pane_slugs):
        pane = get_pane(session_root, pane_slug)
        if pane is None:
            continue
        pane_project_root = str(pane.get("project_root") or session_root)
        if close_worker_pane(pane_project_root, pane_slug, session_root=session_root, force=True):
            closed.append(pane_slug)

    for task_slug, task_state in task_states.items():
        if task_state in {"merged", "failed", "skipped", "cancelled"}:
            continue
        task_states[task_slug] = "cancelled"
        row = task_rows_by_slug.get(task_slug, {})
        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "cancelled",
            str(row.get("agent") or "governor-override"),
            pane_slug=row.get("pane_slug"),
            error="cancelled_by_governor",
            attempt=int(row.get("attempt") or 1),
        )
        cancelled.append(task_slug)

    state_json["state"] = "cancelled"
    state_json["task_states"] = task_states
    update_dag_run(session_root, run_id, status="cancelled", state_json=state_json)
    emit_event(
        session_root,
        "dag_cancelled",
        f"dag/{run_id}",
        dag_run_id=run_id,
        status="cancelled",
    )

    return {"run_id": run_id, "status": "cancelled", "cancelled": cancelled, "closed": closed}


def run_worker_checkpoint(session_root: str, slug: str, message: str) -> None:
    """Executor syscall: record a worker checkpoint."""
    from dgov.persistence import emit_event, set_pane_metadata

    set_pane_metadata(session_root, slug, last_checkpoint=message)
    emit_event(session_root, "checkpoint_created", slug, message=message)


def run_force_complete_dag(session_root: str, run_id: int) -> dict:
    """Executor syscall: force-complete a DAG run, marking all non-terminal tasks as done."""
    from dgov.persistence import emit_event, get_dag_run, update_dag_run, upsert_dag_task

    run = get_dag_run(session_root, run_id)
    if not run:
        return {"error": f"DAG run {run_id} not found"}

    # Reconstruct kernel state and force all non-terminal tasks to MERGED
    state_json = run.get("state_json", {})
    task_states = state_json.get("task_states", {})
    forced = []
    for task_slug, ts in task_states.items():
        if ts not in ("merged", "failed", "skipped"):
            task_states[task_slug] = "merged"
            upsert_dag_task(session_root, run_id, task_slug, "merged", "governor-override")
            forced.append(task_slug)

    state_json["state"] = "completed"
    state_json["task_states"] = task_states
    update_dag_run(session_root, run_id, status="completed", state_json=state_json)
    emit_event(
        session_root, "dag_completed", f"dag/{run_id}", dag_run_id=run_id, status="completed"
    )
    emit_event(
        session_root,
        "evals_verified",
        f"dag/{run_id}",
        dag_run_id=run_id,
        passed=0,
        failed=0,
        total=0,
    )

    return {"run_id": run_id, "forced": forced, "status": "completed"}


def run_skip_dag_task(session_root: str, run_id: int, task_slug: str) -> dict:
    """Executor syscall: skip a single DAG task and let the kernel advance."""
    from dgov.persistence import emit_event, get_dag_run, update_dag_run, upsert_dag_task

    run = get_dag_run(session_root, run_id)
    if not run:
        return {"error": f"DAG run {run_id} not found"}

    state_json = run.get("state_json", {})
    task_states = state_json.get("task_states", {})
    if task_slug not in task_states:
        return {"error": f"Task {task_slug!r} not found in DAG run {run_id}"}
    if task_states[task_slug] in ("merged", "skipped"):
        return {"error": f"Task {task_slug!r} already in terminal state: {task_states[task_slug]}"}

    task_states[task_slug] = "skipped"
    state_json["task_states"] = task_states
    upsert_dag_task(session_root, run_id, task_slug, "skipped", "governor-override")
    update_dag_run(session_root, run_id, state_json=state_json)
    emit_event(
        session_root, "dag_task_completed", f"dag/{run_id}", task=task_slug, dag_run_id=run_id
    )

    return {"run_id": run_id, "task": task_slug, "status": "skipped"}


# ---------------------------------------------------------------------------
# DagKernel runtime adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DagRunResult:
    status: str
    merged: list[str]
    failed: list[str]
    skipped: list[str]
    blocked: list[str]
    run_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class DagReactor:
    """Stateless executor for DagKernel actions."""

    project_root: str
    session_root: str
    run_id: int
    dag: DagDefinition
    progress: Callable[[str], None] = lambda msg: None

    def execute(self, action: DagAction) -> DagEvent | None:
        """Execute a single kernel action and return the resulting event."""
        from dgov.kernel import (
            CloseTask,
            DispatchTask,
            InterruptGovernor,
            MergeTask,
            RetryTask,
            ReviewTask,
            SkipTask,
            TaskClosed,
        )

        if isinstance(action, DispatchTask):
            return _dag_dispatch(
                self.dag, action.task_slug, self.run_id, self.session_root, self.progress
            )

        if isinstance(action, ReviewTask):
            return _dag_review(
                self.dag,
                self.project_root,
                self.session_root,
                action.task_slug,
                action.pane_slug,
                self.progress,
                review_agent=action.review_agent,
                run_id=self.run_id,
            )

        if isinstance(action, MergeTask):
            return _dag_merge(
                self.dag,
                self.project_root,
                self.session_root,
                action.task_slug,
                action.pane_slug,
                self.progress,
            )

        if isinstance(action, SkipTask):
            _dag_skip(
                self.session_root,
                self.run_id,
                action.task_slug,
                self.dag,
                action.reason,
                self.progress,
            )
            return TaskClosed(action.task_slug)

        if isinstance(action, CloseTask):
            _dag_close(
                self.project_root,
                self.session_root,
                action.task_slug,
                action.pane_slug,
                action.reason,
                self.progress,
            )
            return TaskClosed(action.task_slug)

        if isinstance(action, InterruptGovernor):
            _dag_interrupt(
                self.project_root,
                self.session_root,
                self.run_id,
                action.task_slug,
                action.pane_slug,
                action.reason,
                self.progress,
            )
            return None

        if isinstance(action, RetryTask):
            return _dag_retry(
                self.project_root,
                self.session_root,
                self.run_id,
                action.task_slug,
                action.pane_slug,
                action.attempt,
                getattr(self.dag, "default_max_retries", 3),
                self.progress,
            )

        return None


def run_dag_kernel(
    project_root: str,
    dag_definition: DagDefinition,
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
        DagDone,
        DagKernel,
        TaskDispatched,
        TaskRetryStarted,
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
        max_retries=getattr(dag, "default_max_retries", 3),
    )
    actions = kernel.start()

    # Pane slug tracking (task_slug → pane_slug)
    pane_map: dict[str, str] = {}

    # Stable state tracking per pane for poll_once
    stable_states: dict[str, dict] = {}

    # Action queue — process non-blocking actions immediately,
    # only block on WaitForAny.
    queue: list[DagAction] = list(actions)

    def _extend_queue(new_actions: list[DagAction]) -> None:
        # If new actions contain a WaitForAny, drop any existing ones
        # from the queue — the new one has the latest waiting set.
        has_new_wait = any(isinstance(a, WaitForAny) for a in new_actions)
        if has_new_wait:
            queue[:] = [a for a in queue if not isinstance(a, WaitForAny)]
        queue.extend(new_actions)

    reactor = DagReactor(
        project_root=project_root,
        session_root=session_root,
        run_id=run_id,
        dag=dag,
        progress=_progress,
    )

    while queue:
        action = queue.pop(0)

        if isinstance(action, DagDone):
            _progress(
                f"DAG {action.status}: "
                f"{len(action.merged)} merged, "
                f"{len(action.failed)} failed, "
                f"{len(action.skipped)} skipped, "
                f"{len(action.blocked)} blocked"
            )
            return DagRunResult(
                status=action.status,
                merged=list(action.merged),
                failed=list(action.failed),
                skipped=list(action.skipped),
                blocked=list(action.blocked),
                run_id=run_id,
            )

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

        # Execute non-waiting actions through the reactor
        new_event = reactor.execute(action)
        if new_event:
            if isinstance(new_event, TaskDispatched):
                pane_map[action.task_slug] = new_event.pane_slug
            elif isinstance(new_event, TaskRetryStarted):
                pane_map[action.task_slug] = new_event.new_pane_slug

            _extend_queue(kernel.handle(new_event))

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
        blocked=[],
        run_id=run_id,
        error="Kernel queue exhausted without DagDone",
    )


def _dag_dispatch(
    dag: DagDefinition,
    task_slug: str,
    run_id: int,
    session_root: str,
    progress: Callable[[str], None],
) -> TaskDispatched | TaskDispatchFailed:
    """Execute a DispatchTask action. Returns TaskDispatched or TaskDispatchFailed."""
    from dgov.kernel import TaskDispatched, TaskDispatchFailed
    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event, set_pane_metadata, upsert_dag_task

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
            role=task.role,
        )
        pane_slug = pane.slug
        set_pane_metadata(
            session_root,
            pane_slug,
            file_claims=touches,
            commit_message=task.commit_message,
        )
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
            dag_run_id=run_id,
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
    readonly_timeout: float = 30.0,
) -> TaskWaitDone:
    """Poll active panes round-robin until one completes. Returns TaskWaitDone.

    Uses the unified WorkerObservation to check completion — same model
    the monitor uses for classification.

    Bug #185 fix: Workers stuck in non-terminal phases (STUCK, IDLE, WAITING_INPUT)
    for longer than readonly_timeout are considered timed out.
    """
    import time

    from dgov.kernel import TaskWaitDone, WorkerPhase
    from dgov.monitor import observe_worker
    from dgov.persistence import emit_event

    start = time.monotonic()
    max_timeout = max(task_timeouts.get(s, 600) for s in task_slugs)

    # Track when each worker entered a readonly (non-working, non-terminal) phase
    # Bug #185: Detect workers stuck in STUCK/IDLE/WAITING_INPUT
    readonly_phases = frozenset([WorkerPhase.STUCK, WorkerPhase.IDLE, WorkerPhase.WAITING_INPUT])
    readonly_start: dict[str, float] = {}

    while True:
        now = time.monotonic()
        for task_slug in task_slugs:
            pane_slug = pane_map.get(task_slug, "")
            if not pane_slug:
                continue

            obs = observe_worker(project_root, session_root, pane_slug)

            # Terminal phases: immediate completion
            if obs.phase in (WorkerPhase.DONE, WorkerPhase.FAILED, WorkerPhase.UNKNOWN):
                pane_state = PaneState.DONE if obs.phase == WorkerPhase.DONE else PaneState.FAILED
                return TaskWaitDone(task_slug, pane_slug, pane_state)

            # Bug #185: Check for readonly phase timeout
            if obs.phase in readonly_phases:
                if task_slug not in readonly_start:
                    readonly_start[task_slug] = now
                elif (now - readonly_start[task_slug]) > readonly_timeout:
                    # Worker stuck in readonly phase too long - timeout
                    emit_event(
                        session_root,
                        "pane_timed_out",
                        pane_slug,
                        task_slug=task_slug,
                        reason="readonly_phase_timeout",
                        phase=obs.phase.value,
                        elapsed_seconds=now - readonly_start[task_slug],
                    )
                    return TaskWaitDone(task_slug, pane_slug, PaneState.TIMED_OUT)
            else:
                # Working or committing - reset readonly timer
                readonly_start.pop(task_slug, None)

        elapsed = now - start
        if elapsed > max_timeout:
            pane_slug = pane_map.get(task_slugs[0], "")
            emit_event(
                session_root,
                "pane_timed_out",
                pane_slug,
                task_slug=task_slugs[0],
                reason="max_timeout",
                elapsed_seconds=elapsed,
            )
            return TaskWaitDone(task_slugs[0], pane_slug, PaneState.TIMED_OUT)

        from dgov.persistence import _wait_for_notify

        _wait_for_notify(session_root, poll_interval)


def _dag_review(
    dag: DagDefinition,
    project_root: str,
    session_root: str,
    task_slug: str,
    pane_slug: str,
    progress: Callable[[str], None],
    review_agent: str | None = None,
    run_id: int | None = None,
) -> TaskReviewDone:
    """Execute a ReviewTask action. Returns TaskReviewDone."""
    from dgov.kernel import TaskReviewDone

    task = dag.tasks[task_slug]

    if review_agent:
        progress(f"  reviewing {task_slug} with {review_agent}")

    # Look up eval contract from typed persistence (never reparse blobs)
    evals: tuple[dict, ...] = ()
    if run_id is not None:
        try:
            from dgov.persistence import list_dag_evals, list_dag_unit_eval_links

            links = list_dag_unit_eval_links(session_root, run_id)
            eval_ids = {lk["eval_id"] for lk in links if lk["unit_slug"] == task_slug}
            if eval_ids:
                all_evals = list_dag_evals(session_root, run_id)
                evals = tuple(ev for ev in all_evals if ev["eval_id"] in eval_ids)
        except Exception:
            logger.debug("failed to load eval contract for %s", task_slug, exc_info=True)

    result = run_review_only(
        project_root,
        pane_slug,
        session_root=session_root,
        require_safe=True,
        require_commits=True,
        review_agent=review_agent,
        tests_pass=task.tests_pass,
        lint_clean=task.lint_clean,
        post_merge_check=task.post_merge_check,
        evals=evals,
    )
    progress(f"  reviewed {task_slug}: {result.verdict}")
    return TaskReviewDone(
        task_slug,
        passed=result.passed,
        verdict=result.verdict,
        commit_count=result.commit_count,
    )


def _dag_merge(
    dag: DagDefinition,
    project_root: str,
    session_root: str,
    task_slug: str,
    pane_slug: str,
    progress: Callable[[str], None],
) -> TaskMergeDone:
    """Execute a MergeTask action. Returns TaskMergeDone."""
    from dgov.gitops import build_manifest_on_completion, validate_manifest_freshness
    from dgov.kernel import TaskMergeDone
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

    task = dag.tasks[task_slug]
    result = run_merge_only(
        project_root,
        pane_slug,
        session_root=session_root,
        resolve=dag.merge_resolve,
        squash=dag.merge_squash,
        message=task.commit_message or None,
    )
    if result.error:
        progress(f"  merge failed {task_slug}: {result.error}")
    else:
        progress(f"  merged {task_slug}")
    return TaskMergeDone(task_slug, error=result.error)


def _dag_retry(
    project_root: str,
    session_root: str,
    run_id: int,
    task_slug: str,
    pane_slug: str,
    attempt: int,
    max_retries: int,
    progress: Callable[[str], None],
) -> TaskRetryStarted | TaskDispatchFailed:
    """Execute a RetryTask action. Returns TaskRetryStarted or TaskDispatchFailed."""
    from dgov.kernel import TaskDispatchFailed, TaskRetryStarted
    from dgov.persistence import upsert_dag_task
    from dgov.recovery import retry_or_escalate

    progress(f"  retrying {task_slug} (attempt {attempt})")
    try:
        res = retry_or_escalate(
            project_root,
            pane_slug,
            session_root=session_root,
            max_retries=max_retries,
        )

        if res.get("error"):
            return TaskDispatchFailed(task_slug, res["error"])

        new_slug = res.get("new_slug")
        if not new_slug:
            return TaskDispatchFailed(task_slug, "Retry failed to produce new slug")

        target_agent = res.get("agent", "")
        escalated = res.get("action") == "escalate"

        # On escalation, reset attempt so the new tier gets its full retry budget.
        # kernel.handle(TaskRetryStarted) sets attempts[task] = event.attempt + 1.
        effective_attempt = 0 if escalated else attempt

        upsert_dag_task(
            session_root,
            run_id,
            task_slug,
            "dispatched",
            target_agent,
            attempt=effective_attempt + 1,
            pane_slug=new_slug,
        )
        if escalated:
            progress(f"  escalated {task_slug} to {target_agent}")
        return TaskRetryStarted(task_slug, new_slug, effective_attempt)

    except Exception as exc:
        logger.error("Retry failed for %s: %s", task_slug, exc)
        return TaskDispatchFailed(task_slug, str(exc))


def _dag_skip(
    session_root: str,
    run_id: int,
    task_slug: str,
    dag: DagDefinition,
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


def _dag_interrupt(
    project_root: str,
    session_root: str,
    run_id: int,
    task_slug: str,
    pane_slug: str,
    reason: str,
    progress: Callable[[str], None],
) -> None:
    """Gather context for a governor interrupt and emit dag_blocked event."""
    from dgov.inspection import diff_worker_pane
    from dgov.persistence import emit_event, get_pane, update_dag_run, upsert_dag_task
    from dgov.status import tail_worker_log

    progress(f"  INTERRUPT: {task_slug} blocked on {reason}")

    # 1. Gather context
    pane = get_pane(session_root, pane_slug) or {}
    role = pane.get("role", "worker")
    log_tail = tail_worker_log(session_root, pane_slug, lines=20)

    diff_data = diff_worker_pane(project_root, pane_slug, session_root=session_root)
    diff_text = diff_data.get("diff", "")

    interrupt_data = {
        "task_slug": task_slug,
        "pane_slug": pane_slug,
        "role": role,
        "reason": reason,
        "log_tail": log_tail,
        "diff": diff_text,
    }

    # 2. Persist state
    upsert_dag_task(
        session_root,
        run_id,
        task_slug,
        "blocked_on_governor",
        pane.get("agent", "unknown"),
        pane_slug=pane_slug,
        error=reason,
    )

    # 3. Write detailed report
    report_dir = Path(session_root, ".dgov", "reports", "interrupts")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{pane_slug}.json"
    report_path.write_text(json.dumps(interrupt_data, indent=2))

    # 4. Persist run state
    update_dag_run(session_root, run_id, status="blocked")

    # 5. Notify governor
    emit_event(
        session_root,
        "dag_blocked",
        f"dag/{run_id}",
        task=task_slug,
        pane_slug=pane_slug,
        reason=reason,
        report_path=str(report_path),
    )
