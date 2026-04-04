"""Async bridge for DagKernel — high-performance headless orchestration.

Follows Lacustrine Pillars:
- Pillar #1: Separation of Powers - Runner orchestrates; Worker implements.
- Pillar #9: Hot-Path - Zero-latency async signaling, no polling or pipes.
- Pillar #10: Fail-Closed - Graceful shutdown leaves no dangling state.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dgov.actions import (
    DagAction,
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
from dgov.persistence import add_task, emit_event
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.settlement import (
    autofix_sandbox,
    validate_sandbox,
)
from dgov.types import WorkerExit, Worktree
from dgov.workers.headless import run_headless_worker
from dgov.worktree import (
    commit_in_worktree,
    create_worktree,
    merge_worktree,
    remove_worktree,
)

logger = logging.getLogger(__name__)


class EventDagRunner:
    """Async DAG runner — pure event-driven dispatch."""

    def __init__(self, dag: DagDefinition, session_root: str = ".") -> None:
        self.dag = dag
        self.session_root = session_root
        self.deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
        # Build file claims from plan for scope enforcement in review gate
        self.task_files = {
            slug: tuple(dict.fromkeys(t.files.create + t.files.edit + t.files.delete))
            for slug, t in dag.tasks.items()
        }
        self.kernel = DagKernel(deps=self.deps, task_files=self.task_files)
        self._pending_dispatches: set[str] = set()
        self._event_queue: asyncio.Queue[WorkerExit] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=8)
        self._worktrees: dict[str, Worktree] = {}
        self._worker_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown_event = asyncio.Event()

    def _setup_signal_handlers(self) -> None:
        """Install signal handlers for graceful shutdown (Pillar #10)."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

    def _request_shutdown(self) -> None:
        """Signal handler — trigger graceful shutdown."""
        logger.info("Shutdown requested — initiating graceful cleanup")
        self._shutdown_event.set()
        emit_event(
            self.session_root,
            "shutdown_requested",
            "runner",
            reason="signal",
        )

    async def _cleanup(self) -> None:
        """Cleanup all resources — worktrees, executor, connections (Pillar #3, #10)."""
        logger.info(
            "Cleaning up %d worker tasks, %d worktrees",
            len(self._worker_tasks),
            len(self._worktrees),
        )

        # Cancel active worker asyncio tasks first — stop work before removing worktrees
        for task_slug, atask in list(self._worker_tasks.items()):
            if not atask.done():
                atask.cancel()
                logger.debug("Cancelled worker task: %s", task_slug)

        # Wait briefly for cancellations to propagate
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)
        self._worker_tasks.clear()
        self._pending_dispatches.clear()

        # Close all worktrees
        loop = asyncio.get_running_loop()
        for task_slug, wt in list(self._worktrees.items()):
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
                logger.debug("Removed worktree: %s", task_slug)
            except Exception as exc:
                logger.warning("Failed to remove worktree %s: %s", task_slug, exc)

        self._worktrees.clear()

        # Shutdown executor
        self._executor.shutdown(wait=False)
        logger.info("Cleanup complete")

    async def run(self) -> dict[str, str]:
        """Execute DAG with high-performance async loop."""
        self._setup_signal_handlers()

        try:
            return await self._run_loop()
        finally:
            await self._cleanup()

    async def _run_loop(self) -> dict[str, str]:
        """Main event loop — separated for graceful shutdown handling."""
        actions = self.kernel.start()

        while True:
            # 1. Process Actions from Kernel
            if actions:
                dispatch_coros: list[tuple[str, asyncio.coroutines]] = []
                next_actions: list[DagAction] = []

                for action in actions:
                    if isinstance(action, DispatchTask):
                        dispatch_coros.append((action.task_slug, self._dispatch(action)))
                    elif isinstance(action, MergeTask):
                        dispatch_coros.append((action.task_slug, self._merge(action)))
                    elif isinstance(action, ReviewTask):
                        next_actions.extend(self._run_review(action))
                    elif isinstance(action, InterruptGovernor):
                        next_actions.extend(self._handle_interrupt(action))
                    elif isinstance(action, DagDone):
                        return {
                            slug: state.value for slug, state in self.kernel.task_states.items()
                        }

                if dispatch_coros:
                    coros = [c for _, c in dispatch_coros]
                    results = await asyncio.gather(*coros, return_exceptions=True)
                    for (task_slug, _), result in zip(dispatch_coros, results):
                        if isinstance(result, BaseException):
                            logger.error("Dispatch/merge failed for %s: %s", task_slug, result)
                            next_actions.extend(
                                self.kernel.handle(
                                    TaskGovernorResumed(task_slug, GovernorAction.FAIL)
                                )
                            )
                        elif isinstance(result, list):
                            next_actions.extend(result)

                if next_actions:
                    actions = next_actions
                    continue

                actions = []

            if self.kernel.done:
                break

            # Check for shutdown request
            if self._shutdown_event.is_set():
                logger.info("Shutdown detected — breaking main loop")
                break

            # 2. Wait for any Worker to finish (Hot-path)
            try:
                exit_event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=5.0,
                )
                self._pending_dispatches.discard(exit_event.task_slug)
                self._worker_tasks.pop(exit_event.task_slug, None)
                status = TaskState.DONE if exit_event.exit_code == 0 else TaskState.FAILED

                actions = self.kernel.handle(
                    TaskWaitDone(exit_event.task_slug, exit_event.pane_slug, status)
                )
                emit_event(
                    self.session_root,
                    "task_done" if status == TaskState.DONE else "task_failed",
                    exit_event.pane_slug,
                    task_slug=exit_event.task_slug,
                )
                continue

            except asyncio.TimeoutError:
                if not self._pending_dispatches and self.kernel.done:
                    break
                continue

        return {slug: state.value for slug, state in self.kernel.task_states.items()}

    def _run_review(self, action: ReviewTask) -> list[DagAction]:
        """Execute fast review gate — git sanity checks (microseconds)."""
        from dgov.persistence import get_task
        from dgov.settlement import review_sandbox

        wt = self._worktrees.get(action.task_slug)
        if not wt:
            return self.kernel.handle(
                TaskReviewDone(
                    action.task_slug,
                    passed=False,
                    verdict="worktree_missing",
                    commit_count=0,
                )
            )

        task_record = get_task(self.session_root, action.task_slug)
        claimed_files = task_record.get("file_claims") if task_record else None
        review_result = review_sandbox(wt.path, claimed_files=claimed_files)

        emit_event(
            self.session_root,
            "review_pass" if review_result.passed else "review_fail",
            action.pane_slug,
            task_slug=action.task_slug,
            verdict=review_result.verdict,
            error=review_result.error,
        )

        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=review_result.passed,
                verdict=review_result.verdict,
                commit_count=len(review_result.actual_files),
            )
        )

    def _handle_interrupt(self, action: InterruptGovernor) -> list[DagAction]:
        """Decide retry vs fail based on attempt count."""
        attempts = self.kernel.attempts.get(action.task_slug, 0)
        if attempts < self.kernel.max_retries:
            logger.info(
                "Governor interrupt: %s — retry %d/%d",
                action.reason,
                attempts + 1,
                self.kernel.max_retries,
            )
            return self.kernel.handle(TaskGovernorResumed(action.task_slug, GovernorAction.RETRY))
        logger.warning(
            "Governor interrupt: %s — max retries (%d) exceeded, failing",
            action.reason,
            self.kernel.max_retries,
        )
        return self.kernel.handle(TaskGovernorResumed(action.task_slug, GovernorAction.FAIL))

    async def _dispatch(self, action: DispatchTask) -> list[DagAction]:
        """Dispatch task to Atomic Headless Worker (Pillar #2)."""
        import uuid

        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        # Pillar #3: Snapshot Isolation
        wt = await loop.run_in_executor(
            self._executor, create_worktree, self.session_root, action.task_slug
        )
        self._worktrees[action.task_slug] = wt

        try:
            pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
            self._pending_dispatches.add(action.task_slug)

            # Create task record with file claims for scope enforcement
            file_claims = tuple(
                dict.fromkeys(task.files.create + task.files.edit + task.files.delete)
            )
            task_record = WorkerTask(
                slug=action.task_slug,
                prompt=task.prompt,
                agent=task.agent,
                project_root=self.session_root,
                worktree_path=str(wt.path),
                branch_name=wt.branch,
                state=TaskState.ACTIVE,
                file_claims=file_claims,
            )
            add_task(self.session_root, task_record)

            # Atomic transition to WAITING
            kernel_actions = self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

            emit_event(
                self.session_root,
                "dag_task_dispatched",
                pane_slug,
                task_slug=action.task_slug,
                agent=task.agent,
            )

            def _on_worker_exit(task_slug: str, pane_slug: str, exit_code: int) -> None:
                exit_event = WorkerExit(
                    task_slug=task_slug,
                    pane_slug=pane_slug,
                    exit_code=exit_code,
                    output_dir=str(output_dir),
                )
                loop.call_soon_threadsafe(self._event_queue.put_nowait, exit_event)

            worker_task = asyncio.create_task(
                run_headless_worker(
                    self.session_root,
                    action.task_slug,
                    pane_slug,
                    wt.path,
                    task,
                    _on_worker_exit,
                )
            )
            self._worker_tasks[action.task_slug] = worker_task
            return kernel_actions

        except Exception as exc:
            # Worktree created but launch failed — clean up to prevent leak
            logger.error(
                "Dispatch failed for %s after worktree creation: %s", action.task_slug, exc
            )
            self._pending_dispatches.discard(action.task_slug)
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
            except Exception as cleanup_exc:
                logger.warning("Worktree cleanup after dispatch failure: %s", cleanup_exc)
            self._worktrees.pop(action.task_slug, None)
            raise

    async def _merge(self, action: MergeTask) -> list[DagAction]:
        """Commit-or-Kill: Merge worktree branch into base (Pillar #2)."""
        wt = self._worktrees.get(action.task_slug)
        if not wt:
            return self.kernel.handle(TaskMergeDone(action.task_slug))

        loop = asyncio.get_running_loop()
        error = None
        try:
            # 1. Auto-fix (lint fix then format) BEFORE commit — scoped to claimed files
            task = self.dag.tasks[action.task_slug]
            file_claims = action.file_claims
            await loop.run_in_executor(self._executor, autofix_sandbox, wt.path, file_claims)

            # 2. Commit (includes auto-fixes) — stage only claimed files
            msg = task.commit_message or f"feat: completed {action.task_slug}"
            await loop.run_in_executor(self._executor, commit_in_worktree, wt, msg, file_claims)

            # 3. Validate (read-only gate)
            gate_result = await loop.run_in_executor(
                self._executor, validate_sandbox, wt.path, wt.commit, self.session_root
            )

            if not gate_result.passed:
                error = gate_result.error
                logger.warning("REJECTED %s: %s", action.task_slug, error)
            else:
                # 3. Merge
                await loop.run_in_executor(self._executor, merge_worktree, self.session_root, wt)
                logger.info("COMMITTED %s", action.task_slug)

        except Exception as exc:
            logger.error("Merge execution failed for %s: %s", action.task_slug, exc)
            error = str(exc)

        # 4. Cleanup worktree regardless of outcome
        try:
            await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
        except Exception as exc:
            logger.warning("Worktree cleanup failed for %s: %s", action.task_slug, exc)

        self._worktrees.pop(action.task_slug, None)

        actions = self.kernel.handle(TaskMergeDone(action.task_slug, error=error))

        emit_event(
            self.session_root,
            "merge_completed" if not error else "task_merge_failed",
            action.pane_slug,
            task_slug=action.task_slug,
            error=error,
        )
        return actions
