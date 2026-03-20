"""Mission primitive: declarative create-wait-review-merge lifecycle."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from dgov.context_packet import build_context_packet
from dgov.persistence import emit_event

logger = logging.getLogger(__name__)

# Severity ordering (shared with review_fix)
_SEVERITY_LEVELS = {"critical": 0, "medium": 1, "low": 2}


@dataclass
class MissionPolicy:
    """Controls mission behavior."""

    agent: str = "claude"
    permission_mode: str = "bypassPermissions"
    auto_merge: bool = True
    touches: tuple[str, ...] = ()
    review_severity: str = "medium"
    timeout: int = 600
    max_retries: int = 1
    escalate_to: str | None = None


@dataclass
class MissionResult:
    """Final mission outcome."""

    state: str  # "completed" | "failed" | "review_pending" | "reviewed_pass"
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
    from dgov.executor import run_dispatch_preflight, run_post_dispatch_lifecycle
    from dgov.lifecycle import create_worker_pane
    from dgov.strategy import _generate_slug

    policy = policy or MissionPolicy()
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    slug_s: str = slug or _generate_slug(prompt)
    start = time.monotonic()

    def _elapsed() -> float:
        return time.monotonic() - start

    def _fail(error: str) -> MissionResult:
        return _make_failed(slug_s, error, session_root, _elapsed())

    def _mission_phase(phase: str, current_slug: str) -> None:
        if phase == "waiting":
            emit_event(session_root, "mission_waiting", current_slug)
        elif phase == "reviewing":
            emit_event(session_root, "mission_reviewing", current_slug)
        elif phase == "merging":
            emit_event(session_root, "mission_merging", current_slug)
        elif phase == "completed":
            emit_event(session_root, "mission_completed", current_slug)
        elif phase == "failed":
            emit_event(session_root, "mission_failed", current_slug)

    # -- PENDING: preflight --
    emit_event(session_root, "mission_pending", slug_s, agent=policy.agent)
    logger.info("Mission %s: preflight (%s)", slug_s, policy.agent)
    packet = build_context_packet(
        prompt,
        file_claims=list(policy.touches) if policy.touches else None,
    )
    report = run_dispatch_preflight(
        project_root,
        policy.agent,
        packet=packet,
        session_root=session_root,
    )
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
            context_packet=packet,
        )
        slug_s = pane.slug
    except Exception as e:
        return _fail(f"Create failed: {e}")

    logger.info("Mission %s: executing canonical post-dispatch lifecycle", slug_s)
    lifecycle = run_post_dispatch_lifecycle(
        project_root,
        slug_s,
        session_root=session_root,
        timeout=policy.timeout,
        max_retries=policy.max_retries,
        auto_merge=policy.auto_merge,
        permission_mode=policy.permission_mode,
        retry_agent=policy.agent,
        escalate_to=policy.escalate_to,
        phase_callback=_mission_phase,
    )
    slug_s = lifecycle.slug
    review = lifecycle.review or {}
    issues = review.get("issues")
    finding_dicts = [{"description": f} for f in issues] if issues else None

    if lifecycle.state == "failed":
        return MissionResult(
            state="failed",
            slug=slug_s,
            findings=finding_dicts,
            error=lifecycle.error,
            merge_result=lifecycle.merge_result,
            duration_s=_elapsed(),
        )

    if lifecycle.state == "review_pending":
        return MissionResult(
            state="review_pending",
            slug=slug_s,
            findings=finding_dicts,
            duration_s=_elapsed(),
        )

    if lifecycle.state == "reviewed_pass":
        return MissionResult(
            state="reviewed_pass",
            slug=slug_s,
            findings=finding_dicts,
            duration_s=_elapsed(),
        )

    logger.info("Mission %s: completed in %.1fs", slug_s, _elapsed())
    return MissionResult(
        state="completed",
        slug=slug_s,
        merge_result=lifecycle.merge_result,
        findings=finding_dicts,
        duration_s=_elapsed(),
    )
