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


def analyze_scope_status(
    actual_files: frozenset[str],
    claimed_files: Sequence[str] | None = None,
    read_files: Sequence[str] = (),
    scope_ignore_files: Sequence[str] = (),
    session_root: str | None = None,
    task_slug: str | None = None,
    pane_slug: str | None = None,
) -> ScopeStatus:
    """Analyze scope status from explicit inputs.

    Does not run git itself; callers must supply ``actual_files`` from git
    diff or status. Reuses the same ignore and transient-write classification
    logic as settlement so the preview is authoritative.
    """
    claimed_writable = frozenset(claimed_files) if claimed_files else frozenset()
    claimed_readonly = frozenset(read_files)

    # Actual file classification via settlement helpers (no duplication)
    unclaimed_actual = compute_unclaimed_files(actual_files, claimed_writable, scope_ignore_files)
    ignored_actual = (actual_files - claimed_writable) - unclaimed_actual

    # Transient write classification
    transient_paths: set[str] = set()
    unclaimed_transient: frozenset[str] = frozenset()
    ignored_transient: frozenset[str] = frozenset()
    if session_root and task_slug and claimed_files:
        transient_paths = collect_transient_write_paths(session_root, task_slug, pane_slug)
        ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs = (
            split_ignore_entries(scope_ignore_files)
        )
        unclaimed_transient = frozenset(
            filter_unclaimed_non_ignored(
                transient_paths,
                claimed_writable,
                ignored_exact,
                ignored_prefix_dirs,
                ignored_named_dirs,
                ignored_globs,
            )
        )
        ignored_transient = (frozenset(transient_paths) - claimed_writable) - unclaimed_transient

    # Blocking failures — delegated to settlement so rules stay single-source.
    blocking_failure = check_scope(actual_files, claimed_files, scope_ignore_files, read_files)
    if blocking_failure is None:
        blocking_failure = check_transient_scope(
            session_root,
            task_slug,
            pane_slug,
            claimed_files,
            actual_files,
            scope_ignore_files,
        )

    return ScopeStatus(
        claimed_writable=claimed_writable,
        claimed_readonly=claimed_readonly,
        actual_files=actual_files,
        transient_write_paths=frozenset(transient_paths),
        ignored_actual_paths=ignored_actual,
        ignored_transient_paths=ignored_transient,
        unclaimed_actual_paths=unclaimed_actual,
        unclaimed_transient_paths=unclaimed_transient,
        blocking_failure=blocking_failure,
    )
