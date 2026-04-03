"""Shared executor policy for dispatch preflight and merge review gates."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dgov.context_packet import ContextPacket, build_context_packet
from dgov.decision import DecisionRecord, ReviewOutputDecision, ReviewOutputRequest, ReviewVerdict
from dgov.inspection import (
    ReviewInfo,
)
from dgov.merger import ConflictResolveStrategy, MergeError, MergeSuccess
from dgov.persistence import PaneState
from dgov.pane_executor import (
    CleanupAction,  # noqa: F401 - re-exported for public API
    CleanupOnlyResult,  # noqa: F401 - re-exported for public API
    CloseOnlyResult,  # noqa: F401 - re-exported for public API
    StateTransitionResult,  # noqa: F401 - re-exported for public API
    run_cleanup_only,  # noqa: F401 - re-exported for public API
    run_close_only,
    run_complete_pane,  # noqa: F401 - re-exported for public API
    run_fail_pane,  # noqa: F401 - re-exported for public API
    run_mark_reviewed,
    run_worker_checkpoint,  # noqa: F401 - re-exported for public API
)

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
class MergeOnlyResult:
    slug: str
    merge_result: "PaneMergeResult"

    @property
    def error(self) -> str | None:
        return _merge_result_error(self.merge_result)


def _merge_result_error(result: "PaneMergeResult") -> str | None:
    if isinstance(result, MergeError):
        return result.error
    if hasattr(result, "conflicts"):
        hint = getattr(result, "hint", None)
        conflicts = ", ".join(getattr(result, "conflicts", []))
        return hint or f"Merge conflicts: {conflicts}" if conflicts else "Merge conflicts"
    return None
