"""Public Python API for dgov.

Usage::

    from dgov.api import Orchestrator
    from dgov.decision import ReviewVerdict

    orc = Orchestrator("/path/to/repo")
    pane = orc.dispatch("Fix the parser", agent="qwen-35b")
    result = orc.wait(pane.slug)
    review = orc.review(pane.slug)
    if review.verdict == ReviewVerdict.SAFE:
        orc.merge(pane.slug)
    orc.close(pane.slug)

    # Or all-in-one:
    result = orc.land("Fix the parser", agent="qwen-35b")
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dgov.decision import ReviewVerdict


@dataclass
class DispatchResult:
    slug: str
    agent: str
    worktree: str
    branch: str
    error: str | None = None


@dataclass
class WaitResult:
    slug: str
    state: str  # "completed" or "failed"
    error: str | None = None


@dataclass
class ReviewResult:
    slug: str
    verdict: ReviewVerdict
    commit_count: int = 0
    files_changed: int = 0
    tests_passed: bool | None = None
    error: str | None = None


@dataclass
class MergeResult:
    slug: str
    merged: bool = False
    merge_sha: str | None = None
    error: str | None = None


@dataclass
class LandResult:
    slug: str
    state: str  # "completed", "failed", "review_pending"
    merge_sha: str | None = None
    error: str | None = None


class Orchestrator:
    """Programmatic interface to dgov's orchestration engine.

    Wraps the executor functions with a clean, documented API.
    All methods return result dataclasses — no exceptions for business errors.
    """

    def __init__(self, project_root: str, session_root: str | None = None):
        self.project_root = os.path.abspath(project_root)
        self.session_root = os.path.abspath(session_root or project_root)

    def dispatch(
        self,
        prompt: str,
        *,
        agent: str = "qwen-35b",
        slug: str | None = None,
        file_claims: list[str] | None = None,
        permission_mode: str = "bypassPermissions",
    ) -> DispatchResult:
        """Dispatch a worker agent to a new worktree."""
        from dgov.context_packet import build_context_packet
        from dgov.executor import run_dispatch_only

        packet = build_context_packet(prompt, file_claims=file_claims)
        try:
            pane = run_dispatch_only(
                project_root=self.project_root,
                prompt=prompt,
                agent=agent,
                session_root=self.session_root,
                permission_mode=permission_mode,
                slug=slug,
                context_packet=packet,
            )
            return DispatchResult(
                slug=pane.slug,
                agent=pane.agent,
                worktree=pane.worktree_path,
                branch=pane.branch_name,
            )
        except (ValueError, RuntimeError) as exc:
            return DispatchResult(
                slug=slug or "",
                agent=agent,
                worktree="",
                branch="",
                error=str(exc),
            )

    def wait(self, slug: str, *, timeout: int = 600) -> WaitResult:
        """Wait for a worker to complete."""
        from dgov.executor import run_wait_only

        result = run_wait_only(
            self.project_root,
            slug,
            session_root=self.session_root,
            timeout=timeout,
        )
        return WaitResult(
            slug=result.slug,
            state=result.state,
            error=result.error,
        )

    def review(self, slug: str) -> ReviewResult:
        """Review a worker's changes."""
        from dgov.executor import run_review_only

        result = run_review_only(
            self.project_root,
            slug,
            session_root=self.session_root,
        )
        review = result.review or {}
        return ReviewResult(
            slug=result.slug,
            verdict=result.verdict or "unknown",
            commit_count=result.commit_count,
            files_changed=review.get("files_changed", 0),
            tests_passed=review.get("tests_passed"),
            error=result.error,
        )

    def merge(
        self,
        slug: str,
        *,
        squash: bool = True,
        strict_claims: bool = False,
    ) -> MergeResult:
        """Merge a worker's branch into main."""
        from dgov.executor import run_merge_only

        result = run_merge_only(
            self.project_root,
            slug,
            session_root=self.session_root,
            squash=squash,
            strict_claims=strict_claims,
        )
        mr = result.merge_result or {}
        return MergeResult(
            slug=slug,
            merged=bool(mr.get("merged")),
            merge_sha=mr.get("merge_sha"),
            error=result.error or mr.get("error"),
        )

    def close(self, slug: str, *, force: bool = False) -> bool:
        """Close a worker pane and clean up resources."""
        from dgov.executor import run_close_only

        result = run_close_only(
            self.project_root,
            slug,
            session_root=self.session_root,
            force=force,
        )
        return result.closed

    def land(
        self,
        prompt: str,
        *,
        agent: str = "qwen-35b",
        slug: str | None = None,
        file_claims: list[str] | None = None,
        timeout: int = 600,
        strict_claims: bool = False,
        permission_mode: str = "bypassPermissions",
    ) -> LandResult:
        """Dispatch, wait, review, merge, close — all in one.

        Returns a LandResult with the final state.
        """
        from dgov.executor import run_post_dispatch_lifecycle

        dispatch = self.dispatch(
            prompt,
            agent=agent,
            slug=slug,
            file_claims=file_claims,
            permission_mode=permission_mode,
        )
        if dispatch.error:
            return LandResult(slug=dispatch.slug, state="failed", error=dispatch.error)

        lifecycle = run_post_dispatch_lifecycle(
            self.project_root,
            dispatch.slug,
            session_root=self.session_root,
            timeout=timeout,
            auto_merge=True,
            permission_mode=permission_mode,
        )
        mr = lifecycle.merge_result or {}
        return LandResult(
            slug=lifecycle.slug,
            state=lifecycle.state,
            merge_sha=mr.get("merge_sha"),
            error=lifecycle.error,
        )

    def status(self) -> dict:
        """Get current orchestrator status."""
        from dgov.inspection import compute_stats

        return compute_stats(self.session_root)

    def panes(self) -> list[dict]:
        """List all worker panes."""
        from dgov.status import list_worker_panes

        return list_worker_panes(self.project_root, session_root=self.session_root)
