"""Mission primitive: declarative create-wait-review-merge lifecycle."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from dgov.persistence import emit_event

logger = logging.getLogger(__name__)

# Severity ordering (shared with review_fix)
_SEVERITY_LEVELS = {"critical": 0, "medium": 1, "low": 2}


@dataclass
class MissionPolicy:
    """Controls mission behavior."""

    agent: str = "claude"
    permission_mode: str = "bypassPermissions"
    auto_merge: bool = False
    review_severity: str = "medium"
    timeout: int = 600
    max_retries: int = 1
    escalate_to: str | None = None


@dataclass
class MissionResult:
    """Final mission outcome."""

    state: str  # "completed" | "failed" | "review_pending"
    slug: str
    findings: list[dict] | None = None
    error: str | None = None
    merge_result: dict | None = None
    duration_s: float = 0.0


def _has_blocking_findings(findings: list[dict], threshold: str) -> bool:
    """Check if any findings meet or exceed the severity threshold."""
    cutoff = _SEVERITY_LEVELS.get(threshold, 1)
    return any(_SEVERITY_LEVELS.get(f.get("severity", "low"), 2) <= cutoff for f in findings)


def _make_failed(slug: str, error: str, session_root: str, duration_s: float) -> MissionResult:
    emit_event(session_root, "mission_failed", slug, error=error[:200])
    return MissionResult(state="failed", slug=slug, error=error, duration_s=duration_s)


def run_mission(
    project_root: str,
    prompt: str,
    policy: MissionPolicy | None = None,
    session_root: str | None = None,
    slug: str | None = None,
) -> MissionResult:
    """Run a single mission to completion.

    Synchronous state machine: PENDING -> RUNNING -> WAITING -> REVIEWING
    -> MERGING -> COMPLETED (or FAILED / REVIEW_PENDING at any step).
    """
    from dgov.inspection import review_worker_pane
    from dgov.lifecycle import close_worker_pane, create_worker_pane
    from dgov.merger import merge_worker_pane
    from dgov.preflight import run_preflight
    from dgov.recovery import escalate_worker_pane
    from dgov.strategy import _generate_slug
    from dgov.waiter import PaneTimeoutError, wait_worker_pane

    policy = policy or MissionPolicy()
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    slug_s: str = slug or _generate_slug(prompt)
    start = time.monotonic()

    def _elapsed() -> float:
        return time.monotonic() - start

    def _fail(error: str) -> MissionResult:
        return _make_failed(slug_s, error, session_root, _elapsed())

    # -- PENDING: preflight --
    emit_event(session_root, "mission_pending", slug_s, agent=policy.agent)
    logger.info("Mission %s: preflight (%s)", slug_s, policy.agent)
    report = run_preflight(project_root, agent=policy.agent, session_root=session_root)
    if not report.passed:
        failed_checks = [c.message for c in report.checks if not c.passed and c.critical]
        return _fail(f"Preflight failed: {'; '.join(failed_checks)}")

    # -- RUNNING: create worker pane --
    emit_event(session_root, "mission_running", slug_s, agent=policy.agent)
    logger.info("Mission %s: dispatching worker", slug_s)
    try:
        pane = create_worker_pane(
            project_root=project_root,
            prompt=prompt,
            agent=policy.agent,
            permission_mode=policy.permission_mode,
            slug=slug_s,
            session_root=session_root,
        )
        slug_s = pane.slug
    except Exception as e:
        return _fail(f"Create failed: {e}")

    # -- WAITING: wait for worker to finish --
    emit_event(session_root, "mission_waiting", slug_s)
    logger.info("Mission %s: waiting for worker (timeout=%ds)", slug_s, policy.timeout)
    retries_left = policy.max_retries
    while True:
        try:
            wait_worker_pane(
                project_root,
                slug_s,
                session_root=session_root,
                timeout=policy.timeout,
                auto_retry=False,
            )
            break
        except PaneTimeoutError:
            if retries_left > 0 and policy.escalate_to:
                esc_result = escalate_worker_pane(
                    project_root,
                    slug_s,
                    target_agent=policy.escalate_to,
                    session_root=session_root,
                    permission_mode=policy.permission_mode,
                )
                if esc_result.get("error"):
                    close_worker_pane(project_root, slug_s, session_root=session_root)
                    return _fail(f"Escalation failed: {esc_result['error']}")
                slug_s = esc_result["new_slug"]
                retries_left -= 1
                continue
            elif retries_left > 0:
                from dgov.recovery import retry_worker_pane

                retry_result = retry_worker_pane(
                    project_root, slug_s, session_root=session_root, agent=policy.agent
                )
                if retry_result.get("error"):
                    close_worker_pane(project_root, slug_s, session_root=session_root)
                    return _fail(f"Retry failed: {retry_result['error']}")
                slug_s = retry_result["new_slug"]
                retries_left -= 1
                continue
            else:
                close_worker_pane(project_root, slug_s, session_root=session_root)
                return _fail(f"Worker timed out after {policy.timeout}s (retries exhausted)")

    # Check if worker failed (exit file written, nonzero exit code)
    from dgov.persistence import get_pane

    pane_state = get_pane(session_root, slug_s)
    if pane_state and pane_state.get("state") == "failed":
        close_worker_pane(project_root, slug_s, session_root=session_root, force=True)
        return _fail("Worker exited with an error (check logs with: dgov pane logs)")

    # -- REVIEWING: review the diff --
    emit_event(session_root, "mission_reviewing", slug_s)
    logger.info("Mission %s: reviewing diff", slug_s)
    review = review_worker_pane(project_root, slug_s, session_root=session_root)
    if review.get("error"):
        close_worker_pane(project_root, slug_s, session_root=session_root)
        return _fail(f"Review failed: {review['error']}")

    issues = review.get("issues")
    finding_dicts = [{"description": f} for f in issues] if issues else None
    verdict = review.get("verdict", "safe")

    if verdict != "safe" and not policy.auto_merge:
        emit_event(session_root, "mission_reviewing", slug_s, verdict="review_pending")
        return MissionResult(
            state="review_pending",
            slug=slug_s,
            findings=finding_dicts,
            duration_s=_elapsed(),
        )

    # -- MERGING --
    emit_event(session_root, "mission_merging", slug_s)
    logger.info("Mission %s: merging", slug_s)
    merge = merge_worker_pane(project_root, slug_s, session_root=session_root)
    if merge.get("error"):
        close_worker_pane(project_root, slug_s, session_root=session_root)
        return _fail(f"Merge failed: {merge['error']}")

    # -- COMPLETED --
    emit_event(session_root, "mission_completed", slug_s)
    logger.info("Mission %s: completed in %.1fs", slug_s, _elapsed())
    return MissionResult(
        state="completed",
        slug=slug_s,
        merge_result=merge,
        findings=finding_dicts,
        duration_s=_elapsed(),
    )
