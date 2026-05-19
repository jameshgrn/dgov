"""Scope status preview: pure analysis of settlement scope evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dgov.settlement import (
    ReviewResult,
    check_scope,
    check_transient_scope,
    collect_transient_write_paths,
    compute_unclaimed_files,
    filter_unclaimed_non_ignored,
    split_ignore_entries,
)


@dataclass(frozen=True)
class ScopeStatus:
    """Pure analysis of scope evidence without side effects."""

    claimed_writable: frozenset[str]
    claimed_readonly: frozenset[str]
    actual_files: frozenset[str]
    transient_write_paths: frozenset[str]
    ignored_actual_paths: frozenset[str]
    ignored_transient_paths: frozenset[str]
    unclaimed_actual_paths: frozenset[str]
    unclaimed_transient_paths: frozenset[str]
    blocking_failure: ReviewResult | None = None


@dataclass(frozen=True)
class _TransientScope:
    paths: frozenset[str]
    ignored: frozenset[str]
    unclaimed: frozenset[str]


def format_scope_paths(paths: frozenset[str]) -> str:
    """Return sorted comma-separated paths, or '(none)' if empty."""
    return ", ".join(sorted(paths)) or "(none)"


def render_scope_status_lines(status: ScopeStatus) -> list[str]:
    """Return human-readable scope status lines."""
    lines = [
        f"claimed_writable: {format_scope_paths(status.claimed_writable)}",
        f"claimed_readonly: {format_scope_paths(status.claimed_readonly)}",
        f"modified_files: {format_scope_paths(status.actual_files)}",
    ]
    if status.transient_write_paths:
        lines.append(f"transient_writes: {format_scope_paths(status.transient_write_paths)}")
    if status.ignored_actual_paths:
        lines.append(f"ignored_modified: {format_scope_paths(status.ignored_actual_paths)}")
    if status.ignored_transient_paths:
        lines.append(f"ignored_transient: {format_scope_paths(status.ignored_transient_paths)}")
    if status.unclaimed_actual_paths:
        lines.append(f"unclaimed_modified: {format_scope_paths(status.unclaimed_actual_paths)}")
    if status.unclaimed_transient_paths:
        lines.append(
            f"unclaimed_transient: {format_scope_paths(status.unclaimed_transient_paths)}"
        )
    if status.blocking_failure:
        lines.append(f"blocking: {status.blocking_failure.error}")
    else:
        lines.append("blocking: (none)")
    return lines


def analyze_scope_status(
    actual_files: frozenset[str],
    claimed_files: Sequence[str] | None = None,
    read_files: Sequence[str] = (),
    scope_ignore_files: Sequence[str] = (),
    session_root: str | None = None,
    task_slug: str | None = None,
    pane_slug: str | None = None,
) -> ScopeStatus:
    """Analyze explicit scope evidence without running git."""
    claimed_writable = frozenset(claimed_files) if claimed_files else frozenset()
    claimed_readonly = frozenset(read_files)
    unclaimed_actual, ignored_actual = _classify_actual_scope(
        actual_files,
        claimed_writable,
        scope_ignore_files,
    )
    transient = _analyze_transient_scope(
        claimed_files=claimed_files,
        claimed_writable=claimed_writable,
        scope_ignore_files=scope_ignore_files,
        session_root=session_root,
        task_slug=task_slug,
        pane_slug=pane_slug,
    )
    blocking_failure = _blocking_scope_failure(
        actual_files=actual_files,
        claimed_files=claimed_files,
        read_files=read_files,
        scope_ignore_files=scope_ignore_files,
        session_root=session_root,
        task_slug=task_slug,
        pane_slug=pane_slug,
    )

    return _build_scope_status(
        claimed_writable=claimed_writable,
        claimed_readonly=claimed_readonly,
        actual_files=actual_files,
        ignored_actual_paths=ignored_actual,
        unclaimed_actual_paths=unclaimed_actual,
        transient=transient,
        blocking_failure=blocking_failure,
    )


def _build_scope_status(
    *,
    claimed_writable: frozenset[str],
    claimed_readonly: frozenset[str],
    actual_files: frozenset[str],
    ignored_actual_paths: frozenset[str],
    unclaimed_actual_paths: frozenset[str],
    transient: _TransientScope,
    blocking_failure: ReviewResult | None,
) -> ScopeStatus:
    return ScopeStatus(
        claimed_writable=claimed_writable,
        claimed_readonly=claimed_readonly,
        actual_files=actual_files,
        transient_write_paths=transient.paths,
        ignored_actual_paths=ignored_actual_paths,
        ignored_transient_paths=transient.ignored,
        unclaimed_actual_paths=unclaimed_actual_paths,
        unclaimed_transient_paths=transient.unclaimed,
        blocking_failure=blocking_failure,
    )


def _classify_actual_scope(
    actual_files: frozenset[str],
    claimed_writable: frozenset[str],
    scope_ignore_files: Sequence[str],
) -> tuple[frozenset[str], frozenset[str]]:
    unclaimed = compute_unclaimed_files(actual_files, claimed_writable, scope_ignore_files)
    ignored = (actual_files - claimed_writable) - unclaimed
    return unclaimed, ignored


def _analyze_transient_scope(
    *,
    claimed_files: Sequence[str] | None,
    claimed_writable: frozenset[str],
    scope_ignore_files: Sequence[str],
    session_root: str | None,
    task_slug: str | None,
    pane_slug: str | None,
) -> _TransientScope:
    if not session_root or not task_slug or claimed_files is None:
        return _TransientScope(frozenset(), frozenset(), frozenset())

    transient_paths = collect_transient_write_paths(session_root, task_slug, pane_slug)
    ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs = split_ignore_entries(
        scope_ignore_files
    )
    unclaimed = frozenset(
        filter_unclaimed_non_ignored(
            transient_paths,
            claimed_writable,
            ignored_exact,
            ignored_prefix_dirs,
            ignored_named_dirs,
            ignored_globs,
        )
    )
    ignored = (frozenset(transient_paths) - claimed_writable) - unclaimed
    return _TransientScope(frozenset(transient_paths), ignored, unclaimed)


def _blocking_scope_failure(
    *,
    actual_files: frozenset[str],
    claimed_files: Sequence[str] | None,
    read_files: Sequence[str],
    scope_ignore_files: Sequence[str],
    session_root: str | None,
    task_slug: str | None,
    pane_slug: str | None,
) -> ReviewResult | None:
    failure = check_scope(actual_files, claimed_files, scope_ignore_files, read_files)
    if failure is not None:
        return failure
    return check_transient_scope(
        session_root,
        task_slug,
        pane_slug,
        claimed_files,
        actual_files,
        scope_ignore_files,
    )
