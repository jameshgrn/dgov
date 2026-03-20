"""Deterministic kernel primitives for the single-pane lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dgov.executor import CleanupOnlyResult, MergeOnlyResult, ReviewOnlyResult, WaitOnlyResult


class KernelState(StrEnum):
    START = "start"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    MERGING = "merging"
    REVIEW_PENDING = "review_pending"
    REVIEWED_PASS = "reviewed_pass"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class WaitForPane:
    slug: str


@dataclass(frozen=True)
class ReviewPane:
    slug: str


@dataclass(frozen=True)
class MergePane:
    slug: str


@dataclass(frozen=True)
class CleanupPane:
    slug: str
    state: str
    failure_stage: str | None = None


KernelAction = WaitForPane | ReviewPane | MergePane | CleanupPane


@dataclass(frozen=True)
class WaitCompleted:
    result: WaitOnlyResult


@dataclass(frozen=True)
class ReviewCompleted:
    result: ReviewOnlyResult


@dataclass(frozen=True)
class MergeCompleted:
    result: MergeOnlyResult


@dataclass(frozen=True)
class CleanupCompleted:
    result: CleanupOnlyResult


KernelEvent = WaitCompleted | ReviewCompleted | MergeCompleted | CleanupCompleted


@dataclass
class PostDispatchKernel:
    auto_merge: bool = True
    state: KernelState = KernelState.START

    def start(self, slug: str) -> list[KernelAction]:
        if self.state is not KernelState.START:
            raise ValueError(f"Kernel already started in state {self.state}")
        self.state = KernelState.WAITING
        return [WaitForPane(slug)]

    def start_review(self, slug: str) -> list[KernelAction]:
        if self.state is not KernelState.START:
            raise ValueError(f"Kernel already started in state {self.state}")
        self.state = KernelState.REVIEWING
        return [ReviewPane(slug)]

    def handle(self, event: KernelEvent) -> list[KernelAction]:
        match self.state, event:
            case KernelState.WAITING, WaitCompleted(result=result):
                if result.state != "completed":
                    self.state = KernelState.FAILED
                    return [
                        CleanupPane(
                            result.slug,
                            state="failed",
                            failure_stage=result.failure_stage,
                        )
                    ]
                self.state = KernelState.REVIEWING
                return [ReviewPane(result.slug)]

            case KernelState.REVIEWING, ReviewCompleted(result=result):
                if result.error is not None:
                    self.state = KernelState.FAILED
                    return [CleanupPane(result.slug, state="failed", failure_stage="review")]
                if result.verdict != "safe":
                    self.state = KernelState.REVIEW_PENDING
                    return [CleanupPane(result.slug, state="review_pending")]
                if result.commit_count == 0:
                    self.state = KernelState.FAILED
                    return [CleanupPane(result.slug, state="failed", failure_stage="review")]
                if not self.auto_merge:
                    self.state = KernelState.REVIEWED_PASS
                    return [CleanupPane(result.slug, state="review_pending")]
                self.state = KernelState.MERGING
                return [MergePane(result.slug)]

            case KernelState.MERGING, MergeCompleted(result=result):
                if result.error is not None:
                    self.state = KernelState.FAILED
                    return [CleanupPane(result.slug, state="failed", failure_stage="merge")]
                self.state = KernelState.COMPLETED
                return [CleanupPane(result.slug, state="completed")]

            case KernelState.REVIEW_PENDING, CleanupCompleted():
                return []

            case KernelState.REVIEWED_PASS, CleanupCompleted():
                return []

            case KernelState.COMPLETED, CleanupCompleted():
                return []

            case KernelState.FAILED, CleanupCompleted():
                return []

            case _:
                raise ValueError(f"Illegal kernel transition: state={self.state} event={event!r}")
