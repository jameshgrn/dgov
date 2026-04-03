"""Async bridge for DagKernel — high-performance headless orchestration.

Follows Lacustrine Pillars:
- Pillar #1: Separation of Powers - Runner orchestrates; Worker implements.
- Pillar #9: Hot-Path - Zero-latency async signaling, no polling or pipes.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dgov.actions import (
    DagDone,
    DispatchTask,
    GovernorAction,
    InterruptGovernor,
    MergeTask,
    ReviewTask,
    TaskDispatched,
    TaskGovernorResumed,
    TaskMergeDone,
    TaskReviewDone,
    TaskWaitDone,
)
from dgov.dag_parser import DagDefinition
from dgov.kernel import DagKernel
from dgov.persistence import emit_event
from dgov.settlement import validate_sandbox
from dgov.types import PaneState
from dgov.workers.headless import run_headless_worker
from dgov.worktree import (
    Worktree,
    commit_in_worktree,
    create_worktree,
    merge_worktree,
    remove_worktree,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerExit:
    """Worker exit event — source of truth for completion."""

    task_slug: str
    pane_slug: str
    exit_code: int
    output_dir: str


class EventDagRunner:
    """Async DAG runner — pure event-driven dispatch."""

    def __init__(self, dag: DagDefinition, session_root: str = "."):
        self.dag = dag
        self.session_root = session_root
        self.deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
        self.kernel = DagKernel(deps=self.deps)
        self._pending_dispatches: set[str] = set()
        self._event_queue: asyncio.Queue[WorkerExit] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=8)
        self._worktrees: dict[str, Worktree] = {}

    async def run(self) -> dict[str, str]:
        """Execute DAG with high-performance async loop."""
        actions = self.kernel.start()

        while True:
            dispatch_coros = []
            next_actions = []

            # 1. Process Actions from Kernel
            for action in actions:
                if isinstance(action, DispatchTask):
                    dispatch_coros.append(self._dispatch(action))
                elif isinstance(action, MergeTask):
                    dispatch_coros.append(self._merge(action))
                elif isinstance(action, InterruptGovernor):
                    logger.info("Governor interrupt: %s (auto-retry)", action.reason)
                    next_actions.extend(
                        self.kernel.handle(
                            TaskGovernorResumed(action.task_slug, GovernorAction.RETRY)
                        )
                    )
                elif isinstance(action, DagDone):
                    return {slug: state.value for slug, state in self.kernel.task_states.items()}

            if dispatch_coros:
                await asyncio.gather(*dispatch_coros)

            # If dispatch/merge produced immediate new work, loop again
            if next_actions:
                actions = next_actions
                continue

            if self.kernel.done:
                break

            # 2. Wait for any Worker to finish (Hot-path)
            try:
                exit_event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=5.0,
                )
                self._pending_dispatches.discard(exit_event.task_slug)
                status = PaneState.DONE if exit_event.exit_code == 0 else PaneState.FAILED

                # Update kernel and get next actions
                actions = self.kernel.handle(
                    TaskWaitDone(exit_event.task_slug, exit_event.pane_slug, status)
                )

                # Auto-approve successful reviews for happy path
                review_actions = [a for a in actions if isinstance(a, ReviewTask)]
                for ra in review_actions:
                    actions = self.kernel.handle(
                        TaskReviewDone(
                            ra.task_slug, passed=True, verdict="auto-pass", commit_count=1
                        )
                    )

            except asyncio.TimeoutError:
                if not self._pending_dispatches and self.kernel.done:
                    break
                # Check if stalled, otherwise keep waiting
                actions = []
                continue

        return {slug: state.value for slug, state in self.kernel.task_states.items()}

    async def _dispatch(self, action: DispatchTask) -> None:
        """Dispatch task to Atomic Headless Worker (Pillar #2)."""
        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)

        import uuid

        # Pillar #3: Snapshot Isolation via dedicated worktree
        wt = await asyncio.get_event_loop().run_in_executor(
            self._executor, create_worktree, self.session_root, action.task_slug
        )
        self._worktrees[action.task_slug] = wt

        pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
        self._pending_dispatches.add(action.task_slug)

        # Atomic transition to WAITING
        self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

        emit_event(
            self.session_root,
            "dag_task_dispatched",
            pane_slug,
            task_slug=action.task_slug,
            agent=task.agent,
        )

        def _on_worker_exit(task_slug: str, pane_slug: str, exit_code: int):
            exit_event = WorkerExit(
                task_slug=task_slug,
                pane_slug=pane_slug,
                exit_code=exit_code,
                output_dir=str(output_dir),
            )
            asyncio.get_event_loop().call_soon_threadsafe(self._event_queue.put_nowait, exit_event)

        # Start background worker
        asyncio.create_task(
            run_headless_worker(
                self.session_root,
                action.task_slug,
                pane_slug,
                wt.path,
                task,
                None,  # pipe_path no longer used
                asyncio.get_event_loop(),
                _on_worker_exit,
            )
        )

    async def _merge(self, action: MergeTask) -> None:
        """Commit-or-Kill: Merge worktree branch into base (Pillar #2)."""
        wt = self._worktrees.get(action.task_slug)
        if not wt:
            self.kernel.handle(TaskMergeDone(action.task_slug))
            return

        error = None
        try:
            # 1. Commit everything in the sandbox
            task = self.dag.tasks[action.task_slug]
            msg = task.commit_message or f"feat: completed {action.task_slug}"

            await asyncio.get_event_loop().run_in_executor(
                self._executor, commit_in_worktree, wt, msg
            )

            # 2. Pillar #8: Falsifiable Validation (The Gate)
            gate_result = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                validate_sandbox,
                wt.path,
                wt.commit,
                self.session_root
            )

            if not gate_result.passed:
                error = gate_result.error
                logger.warning("REJECTED %s: %s", action.task_slug, error)
            else:
                await asyncio.get_event_loop().run_in_executor(
                    self._executor, merge_worktree, self.session_root, wt
                )
                logger.info("COMMITTED %s", action.task_slug)

            # 3. Cleanup Sandbox (Pillar #10)
            await asyncio.get_event_loop().run_in_executor(
                self._executor, remove_worktree, self.session_root, wt
            )

        except Exception as exc:
            logger.error("Merge execution failed for %s: %s", action.task_slug, exc)
            error = str(exc)

        self.kernel.handle(TaskMergeDone(action.task_slug, error=error))
