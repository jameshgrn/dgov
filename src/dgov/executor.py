"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

from dataclasses import dataclass

from dgov.context_packet import ContextPacket, build_context_packet


@dataclass(frozen=True)
class ReviewGate:
    review: dict
    passed: bool
    verdict: str
    commit_count: int
    error: str | None = None


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
