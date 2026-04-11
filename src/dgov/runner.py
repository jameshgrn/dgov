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
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dgov.actions import (
    CleanupTask,
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
from dgov.dag_parser import DagDefinition, DagTaskSpec
from dgov.kernel import DagKernel
from dgov.persistence import add_task, emit_event, update_task_state
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.settlement import (
    autofix_sandbox,
    review_sandbox,
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

    def __init__(
        self,
        dag: DagDefinition,
        session_root: str = ".",
        on_event: Callable[[str, str, object], None] | None = None,
        restart: bool = False,
        continue_failed: bool = False,
    ) -> None:
        from dgov.config import load_project_config
        from dgov.persistence import reset_plan_state

        self.dag = dag
        self.session_root = session_root
        self.on_event = on_event
        self.project_config = load_project_config(session_root)
        self.deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
        # Build file claims from plan for scope enforcement in review gate
        self.task_files = {
            slug: tuple(
                dict.fromkeys(t.files.create + t.files.edit + t.files.delete + t.files.touch)
            )
            for slug, t in dag.tasks.items()
        }
        self.kernel = DagKernel(
            deps=self.deps,
            task_files=self.task_files,
            max_retries=dag.default_max_retries,
        )
        self._pending_dispatches: set[str] = set()
        self._event_queue: asyncio.Queue[WorkerExit] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=8)
        self._worktrees: dict[str, Worktree] = {}
        self._rejected_worktrees: dict[str, Worktree] = {}  # Preserved for inspection
        self._worker_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_errors: dict[str, str] = {}
        self._task_start_times: dict[str, float] = {}
        self._task_durations: dict[str, float] = {}
        self._task_timeouts: dict[str, float] = {}
        self._shutdown_event = asyncio.Event()

        if restart:
            reset_plan_state(session_root, dag.name)
        else:
            self._rehydrate()
            self._cleanup_orphaned_actives()
            if continue_failed:
                self._resume_failed()

    def _cleanup_orphaned_actives(self) -> None:
        """Abandon any ACTIVE tasks left over from a crashed prior run.

        After rehydration, ACTIVE tasks have no live worker — they are orphans.
        Mark them ABANDONED so --continue can retry them, and a bare run doesn't
        deadlock waiting for workers that will never finish.
        """

        for slug, state in list(self.kernel.task_states.items()):
            if state == TaskState.ACTIVE:
                logger.warning(
                    "Orphaned ACTIVE task after rehydration: %s — marking ABANDONED", slug
                )
                self.kernel.handle(TaskWaitDone(slug, "cleanup", TaskState.ABANDONED))
                update_task_state(self.session_root, slug, TaskState.ABANDONED.value, force=True)
                emit_event(
                    self.session_root,
                    "task_abandoned",
                    "cleanup",
                    plan_name=self.dag.name,
                    task_slug=slug,
                )

    def _resume_failed(self) -> None:
        """Move all FAILED/ABANDONED/TIMED_OUT/SKIPPED tasks back to PENDING for retry."""
        logger.info("Resuming failed tasks")
        for slug, state in list(self.kernel.task_states.items()):
            if state in (
                TaskState.FAILED,
                TaskState.ABANDONED,
                TaskState.TIMED_OUT,
                TaskState.SKIPPED,
            ):
                logger.info("Resuming task: %s (prior state: %s)", slug, state)
                self.kernel.handle(TaskGovernorResumed(slug, GovernorAction.RETRY))
                emit_event(
                    self.session_root,
                    "dag_task_governor_resumed",
                    "runner",
                    plan_name=self.dag.name,
                    task_slug=slug,
                    action=GovernorAction.RETRY.value,
                )

    def _rehydrate(self) -> None:
        """Replay past events for this plan to restore kernel state."""
        from dgov.persistence import read_events

        events = read_events(self.session_root, plan_name=self.dag.name)
        for ev in events:
            ename = ev["event"]
            task_slug = ev.get("task_slug")
            pane = ev["pane"]

            if not task_slug or task_slug not in self.kernel.task_states:
                continue

            if ename == "dag_task_dispatched":
                self.kernel.handle(TaskDispatched(task_slug, pane))
            elif ename == "task_done":
                self.kernel.handle(TaskWaitDone(task_slug, pane, TaskState.DONE))
            elif ename == "task_abandoned":
                self.kernel.handle(TaskWaitDone(task_slug, pane, TaskState.ABANDONED))
            elif ename == "task_failed":
                # Check error string for specific terminal states like TIMED_OUT
                error = ev.get("error", "").lower()
                status = TaskState.FAILED
                if "timeout" in error:
                    status = TaskState.TIMED_OUT
                self.kernel.handle(TaskWaitDone(task_slug, pane, status))
            elif ename == "review_pass":
                self.kernel.handle(
                    TaskReviewDone(task_slug, passed=True, verdict="rehydrated", commit_count=1)
                )
            elif ename == "review_fail":
                self.kernel.handle(
                    TaskReviewDone(task_slug, passed=False, verdict="rehydrated", commit_count=0)
                )
            elif ename == "merge_completed":
                self.kernel.handle(TaskMergeDone(task_slug, error=None))
            elif ename == "task_merge_failed":
                self.kernel.handle(
                    TaskMergeDone(task_slug, error=ev.get("error", "unknown error"))
                )
            elif ename == "dag_task_governor_resumed":
                # Restore attempt counts and retry/skip/fail state
                action_str = ev.get("action")
                if action_str:
                    try:
                        action = GovernorAction(action_str)
                        self.kernel.handle(TaskGovernorResumed(task_slug, action))
                    except ValueError:
                        pass

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
            plan_name=self.dag.name,
            reason="signal",
        )

    async def _cleanup(self) -> None:
        """Cleanup all resources — worktrees, executor, connections (Pillar #3, #10)."""
        logger.info(
            "Cleaning up %d worker tasks, %d worktrees",
            len(self._worker_tasks),
            len(self._worktrees) + len(self._rejected_worktrees),
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

        # Close all worktrees (including rejected ones for total cleanup ONLY if shutdown was set)
        loop = asyncio.get_running_loop()
        to_clean = list(self._worktrees.values())
        if self._shutdown_event.is_set():
            to_clean += list(self._rejected_worktrees.values())

        for wt in to_clean:
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
            except Exception as exc:
                logger.warning("Failed to remove worktree: %s", exc)

        self._worktrees.clear()
        if self._shutdown_event.is_set():
            self._rejected_worktrees.clear()

        # Shutdown executor
        self._executor.shutdown(wait=False)
        logger.info("Cleanup complete")

    async def run(self) -> dict[str, str]:
        """Execute DAG with high-performance async loop."""
        self._setup_signal_handlers()
        await self._check_model_env()

        # Emit a run-start marker so dgov plan review can scope events to
        # the current invocation. Events persist across runs unless --restart
        # is passed, so review needs an explicit lower bound.
        emit_event(
            self.session_root,
            "run_start",
            f"run-{self.dag.name}",
            plan_name=self.dag.name,
        )

        try:
            return await self._run_loop()
        finally:
            await self._cleanup()

    async def _check_model_env(self) -> None:
        """Check configured OpenAI-compatible API key is set before dispatch."""
        import os

        key_env = self.project_config.llm_api_key_env
        if not os.environ.get(key_env):
            raise RuntimeError(f"{key_env} not set")

    def _task_state_snapshot(self) -> dict[str, str]:
        return {slug: state.value for slug, state in self.kernel.task_states.items()}

    async def _gather_dispatch_results(
        self, dispatch_coros: list[tuple[str, Coroutine[Any, Any, list[DagAction]]]]
    ) -> list[DagAction]:
        """Await dispatch/merge coros, convert exceptions to FAIL actions."""
        next_actions: list[DagAction] = []
        coros: list[Coroutine[Any, Any, list[DagAction]]] = [c for _, c in dispatch_coros]
        results = await asyncio.gather(*coros, return_exceptions=True)  # type: ignore[call-overload]
        for (task_slug, _), result in zip(dispatch_coros, results, strict=False):
            if isinstance(result, BaseException):
                logger.error("Dispatch/merge failed for %s: %s", task_slug, result)
                next_actions.extend(
                    self.kernel.handle(TaskGovernorResumed(task_slug, GovernorAction.FAIL))
                )
            elif isinstance(result, list):
                next_actions.extend(result)
        return next_actions

    async def _process_actions(
        self, actions: list[DagAction]
    ) -> tuple[list[DagAction], dict[str, str] | None]:
        """Fan out kernel actions. Returns (next_actions, final_result_if_done)."""
        dispatch_coros: list[tuple[str, Coroutine[Any, Any, list[DagAction]]]] = []
        next_actions: list[DagAction] = []

        for action in actions:
            if isinstance(action, DispatchTask):
                dispatch_coros.append((action.task_slug, self._dispatch(action)))
            elif isinstance(action, MergeTask):
                dispatch_coros.append((action.task_slug, self._merge(action)))
            elif isinstance(action, ReviewTask):
                next_actions.extend(self._run_review(action))
            elif isinstance(action, CleanupTask):
                next_actions.extend(await self._cleanup_task(action))
            elif isinstance(action, InterruptGovernor):
                next_actions.extend(self._handle_interrupt(action))
            elif isinstance(action, DagDone):
                return [], self._task_state_snapshot()

        if dispatch_coros:
            next_actions.extend(await self._gather_dispatch_results(dispatch_coros))
        return next_actions, None

    async def _cleanup_task(self, action: CleanupTask) -> list[DagAction]:
        """Remove worktree for a task (terminal failure/skip)."""
        wt = self._worktrees.pop(action.task_slug, None)
        if wt:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
                logger.debug("Cleaned up worktree for failed task: %s", action.task_slug)
            except Exception as exc:
                logger.warning("CleanupTask failed for %s: %s", action.task_slug, exc)
        # Sync kernel's terminal state (FAILED/ABANDONED/TIMED_OUT/SKIPPED) to DB
        kernel_state = self.kernel.task_states.get(action.task_slug)
        if kernel_state and kernel_state in (
            TaskState.FAILED,
            TaskState.ABANDONED,
            TaskState.TIMED_OUT,
            TaskState.SKIPPED,
        ):
            try:
                update_task_state(
                    self.session_root, action.task_slug, kernel_state.value, force=True
                )
            except Exception as exc:
                logger.warning("DB state sync failed for %s: %s", action.task_slug, exc)
        return []

    def _handle_worker_exit(self, exit_event: WorkerExit) -> list[DagAction]:
        """Convert a worker exit into kernel actions, recording errors and emitting events."""
        self._pending_dispatches.discard(exit_event.task_slug)
        self._worker_tasks.pop(exit_event.task_slug, None)
        status = TaskState.DONE if exit_event.exit_code == 0 else TaskState.FAILED

        if exit_event.last_error:
            self._task_errors[exit_event.task_slug] = exit_event.last_error

        # Calculate task duration
        start_time = self._task_start_times.pop(exit_event.task_slug, None)
        duration = round(time.time() - start_time, 2) if start_time else None
        if duration is not None:
            self._task_durations[exit_event.task_slug] = duration

        actions = self.kernel.handle(
            TaskWaitDone(exit_event.task_slug, exit_event.pane_slug, status)
        )
        emit_event(
            self.session_root,
            "task_done" if status == TaskState.DONE else "task_failed",
            exit_event.pane_slug,
            plan_name=self.dag.name,
            task_slug=exit_event.task_slug,
            error=exit_event.last_error if status == TaskState.FAILED else None,
            duration=duration,
        )
        return actions

    async def _run_loop(self) -> dict[str, str]:
        """Main event loop — separated for graceful shutdown handling."""
        actions = self.kernel.start()

        while True:
            if actions:
                next_actions, final = await self._process_actions(actions)
                if final is not None:
                    return final
                if next_actions:
                    actions = next_actions
                    continue
                actions = []

            if self.kernel.done or self._shutdown_event.is_set():
                break

            # Wait for any worker to finish (hot-path)
            try:
                exit_event = await asyncio.wait_for(self._event_queue.get(), timeout=5.0)
                actions = self._handle_worker_exit(exit_event)
            except TimeoutError:
                if not self._pending_dispatches and self.kernel.done:
                    break

        return self._task_state_snapshot()

    def _run_review(self, action: ReviewTask) -> list[DagAction]:
        """Execute fast review gate — git sanity checks (microseconds)."""

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

        # Always use the current plan's file claims for review, ensuring resumed
        # tasks honor the most recent recompiled scope.
        claimed_files = self.task_files.get(action.task_slug)
        review_result = review_sandbox(
            wt.path,
            claimed_files=claimed_files,
            project_root=self.session_root,
            task_slug=action.task_slug,
            scope_ignore_files=self.project_config.scope_ignore_files,
        )

        emit_event(
            self.session_root,
            "review_pass" if review_result.passed else "review_fail",
            action.pane_slug,
            plan_name=self.dag.name,
            task_slug=action.task_slug,
            verdict=review_result.verdict,
            error=review_result.error,
        )

        if not review_result.passed and review_result.error:
            error_msg = f"review:{review_result.verdict} — {review_result.error}"
            if review_result.verdict == "scope_violation":
                error_msg += (
                    f"\nhint: add these paths to files.edit in task"
                    f" '{action.task_slug}', then recompile and re-run"
                )
            self._task_errors[action.task_slug] = error_msg

        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=review_result.passed,
                verdict=review_result.verdict,
                commit_count=len(review_result.actual_files),
            )
        )

    _NON_RETRYABLE_ERRORS = frozenset({
        "Agent stopped without calling 'done'",
    })

    def _handle_interrupt(self, action: InterruptGovernor) -> list[DagAction]:
        """Decide retry vs fail based on attempt count."""
        attempts = self.kernel.attempts.get(action.task_slug, 0)
        error_detail = self._task_errors.get(action.task_slug, "")

        gov_action = GovernorAction.FAIL
        if error_detail in self._NON_RETRYABLE_ERRORS:
            logger.error(
                "Task %s failed — non-retryable: %s",
                action.task_slug,
                error_detail,
            )
        elif attempts < self.kernel.max_retries:
            logger.info(
                "Task %s failed — retry %d/%d: %s",
                action.task_slug,
                attempts + 1,
                self.kernel.max_retries,
                error_detail or action.reason,
            )
            gov_action = GovernorAction.RETRY
        else:
            logger.error(
                "Task %s failed — max retries (%d) exceeded: %s",
                action.task_slug,
                self.kernel.max_retries,
                error_detail or action.reason,
            )

        emit_event(
            self.session_root,
            "dag_task_governor_resumed",
            action.pane_slug,
            plan_name=self.dag.name,
            task_slug=action.task_slug,
            action=gov_action.value,
        )
        return self.kernel.handle(TaskGovernorResumed(action.task_slug, gov_action))

    async def _run_with_timeout(
        self,
        task_slug: str,
        pane_slug: str,
        worktree_path: Path,
        task: DagTaskSpec,
        on_exit: Callable[[str, str, int, str], None],
        timeout_s: int,
    ) -> None:
        """Run headless worker with wall-clock timeout enforcement."""
        try:
            await asyncio.wait_for(
                run_headless_worker(
                    self.session_root,
                    task_slug,
                    pane_slug,
                    worktree_path,
                    task,
                    on_exit,
                    on_event=self.on_event,
                ),
                timeout=float(timeout_s) if timeout_s > 0 else None,
            )
        except TimeoutError:
            logger.error("Task %s timed out after %ds", task_slug, timeout_s)
            emit_event(
                self.session_root,
                "task_failed",
                pane_slug,
                plan_name=self.dag.name,
                task_slug=task_slug,
                error=f"Wall-clock timeout after {timeout_s}s",
            )
            on_exit(task_slug, pane_slug, 1, f"Timed out after {timeout_s}s")

    async def _dispatch(self, action: DispatchTask) -> list[DagAction]:
        """Dispatch task to Atomic Headless Worker (Pillar #2)."""
        import uuid

        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        # Enrich prompt with prior failure context on retry
        prompt = task.prompt
        prior_error = self._task_errors.get(action.task_slug)
        if prior_error:
            attempt = self.kernel.attempts.get(action.task_slug, 0)
            prompt = (
                f"PREVIOUS ATTEMPT ({attempt}) FAILED:\n{prior_error}\n\n"
                f"Fix the issue described above, then complete the original task.\n\n"
                f"ORIGINAL TASK:\n{task.prompt}"
            )

        # Pillar #3: Snapshot Isolation
        wt = await loop.run_in_executor(
            self._executor, create_worktree, self.session_root, action.task_slug
        )
        self._worktrees[action.task_slug] = wt

        # Re-resolve agent from project config mapping
        agent = task.agent
        if agent in self.project_config.agents:
            agent = self.project_config.agents[agent]

        try:
            pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
            self._pending_dispatches.add(action.task_slug)
            self._task_start_times[action.task_slug] = time.time()

            # Create task record with file claims for scope enforcement
            file_claims = tuple(
                dict.fromkeys(
                    task.files.create + task.files.edit + task.files.delete + task.files.touch
                )
            )
            task_record = WorkerTask(
                slug=action.task_slug,
                prompt=prompt,
                agent=agent,
                project_root=self.session_root,
                worktree_path=str(wt.path),
                branch_name=wt.branch,
                role=task.role,
                state=TaskState.ACTIVE,
                plan_name=self.dag.name,
                file_claims=file_claims,
            )
            add_task(self.session_root, task_record)

            # Atomic transition to WAITING
            kernel_actions = self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

            emit_event(
                self.session_root,
                "dag_task_dispatched",
                pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                agent=agent,
            )

            def _on_worker_exit(
                task_slug: str, pane_slug: str, exit_code: int, last_error: str = ""
            ) -> None:
                exit_event = WorkerExit(
                    task_slug=task_slug,
                    pane_slug=pane_slug,
                    exit_code=exit_code,
                    output_dir=str(output_dir),
                    last_error=last_error,
                )
                loop.call_soon_threadsafe(self._event_queue.put_nowait, exit_event)

            dispatch_task = task if not prior_error else task.model_copy(update={"prompt": prompt})
            # Use re-resolved agent
            dispatch_task = dispatch_task.model_copy(update={"agent": agent})
            timeout_s = task.timeout_s
            worker_task = asyncio.create_task(
                self._run_with_timeout(
                    action.task_slug,
                    pane_slug,
                    wt.path,
                    dispatch_task,
                    _on_worker_exit,
                    timeout_s,
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

    async def _settle_and_merge(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[str | None, bool]:
        """Run autofix → commit → validate → merge. Returns (error, was_settlement)."""
        task = self.dag.tasks[action.task_slug]
        file_claims = action.file_claims
        pc = self.project_config

        # Researcher tasks are read-only by construction: no edits, no commits,
        # no settlement gates. The `done` summary is already in the event log.
        # Record the research against the HEAD sha so plan status can show it
        # as deployed without inventing a fake commit. See ledger bug #27.
        if task.role == "researcher":
            from dgov import deploy_log

            deploy_log.append(self.session_root, self.dag.name, action.task_slug, wt.commit)
            logger.info("RESEARCHED %s", action.task_slug)
            return None, False

        await loop.run_in_executor(self._executor, autofix_sandbox, wt.path, file_claims, pc)

        msg = task.commit_message or f"feat: completed {action.task_slug}"
        await loop.run_in_executor(self._executor, commit_in_worktree, wt, msg, file_claims)

        gate_result = await loop.run_in_executor(
            self._executor, validate_sandbox, wt.path, wt.commit, self.session_root, pc
        )

        if not gate_result.passed:
            return gate_result.error, True

        merge_sha = await loop.run_in_executor(
            self._executor, merge_worktree, self.session_root, wt
        )
        logger.info("COMMITTED %s", action.task_slug)

        from dgov import deploy_log

        deploy_log.append(self.session_root, self.dag.name, action.task_slug, merge_sha)
        return None, False

    async def _settlement_retry(
        self,
        action: MergeTask,
        wt: Worktree,
        settlement_error: str,
    ) -> None:
        """Re-launch worker in same worktree with settlement error as feedback."""
        import subprocess as sp

        # Reset the failed commit so worker sees uncommitted changes
        sp.run(["git", "reset", "HEAD~1"], cwd=wt.path, capture_output=True)

        task = self.dag.tasks[action.task_slug]
        feedback_prompt = (
            "Your previous attempt was REJECTED by settlement. "
            "Fix the issue and call done.\n\n"
            f"SETTLEMENT ERROR:\n{settlement_error}\n\n"
            f"ORIGINAL TASK:\n{task.prompt}\n\n"
            "The worktree has your changes (uncommitted). "
            "Use git_diff to see them, fix the problem, then call done."
        )

        from dgov.dag_parser import DagTaskSpec

        retry_task = DagTaskSpec(
            slug=action.task_slug,
            summary=f"[retry] {task.summary}",
            prompt=feedback_prompt,
            commit_message=task.commit_message,
            depends_on=task.depends_on,
            files=task.files,
            agent=task.agent,
            timeout_s=task.timeout_s,
        )

        pane_slug = action.pane_slug + "-retry"

        def _noop_exit(slug: str, pane: str, code: int, err: str = "") -> None:
            if code != 0:
                logger.warning("Settlement retry worker exited %d for %s: %s", code, slug, err)

        await run_headless_worker(
            self.session_root,
            action.task_slug,
            pane_slug,
            wt.path,
            retry_task,
            _noop_exit,
            on_event=self.on_event,
        )

    async def _merge(self, action: MergeTask) -> list[DagAction]:
        """Commit-or-Kill: Merge worktree branch into base (Pillar #2)."""
        wt = self._worktrees.get(action.task_slug)
        if not wt:
            return self.kernel.handle(TaskMergeDone(action.task_slug))

        loop = asyncio.get_running_loop()
        error = None
        settlement_rejected = False

        try:
            error, settlement_rejected = await self._settle_and_merge(action, wt, loop)

            # Settlement retry: give worker one chance to fix
            if settlement_rejected and error:
                logger.info(
                    "SETTLEMENT RETRY %s — feeding error back to worker",
                    action.task_slug,
                )
                emit_event(
                    self.session_root,
                    "settlement_retry",
                    action.pane_slug,
                    plan_name=self.dag.name,
                    task_slug=action.task_slug,
                    error=error,
                )
                await self._settlement_retry(action, wt, error)
                error, settlement_rejected = await self._settle_and_merge(action, wt, loop)
                if settlement_rejected:
                    logger.warning("REJECTED %s after retry: %s", action.task_slug, error)

        except Exception as exc:
            logger.error("Merge execution failed for %s: %s", action.task_slug, exc)
            error = str(exc)

        # Cleanup — keep worktree on settlement rejection so governor can inspect
        if not settlement_rejected:
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
            except Exception as exc:
                logger.warning("Worktree cleanup failed for %s: %s", action.task_slug, exc)
            self._worktrees.pop(action.task_slug, None)
        else:
            logger.info(
                "Worktree preserved for inspection: %s (%s)",
                action.task_slug,
                wt.path,
            )
            # Move to rejected dict so _cleanup skips it during normal task lifecycle
            # but can still clean it up on process exit.
            self._worktrees.pop(action.task_slug, None)
            self._rejected_worktrees[action.task_slug] = wt

        if error:
            self._task_errors[action.task_slug] = error

        actions = self.kernel.handle(TaskMergeDone(action.task_slug, error=error))

        # Sync terminal state to DB so dgov status reflects reality
        db_state = TaskState.MERGED if not error else TaskState.FAILED
        try:
            update_task_state(self.session_root, action.task_slug, db_state.value, force=True)
        except Exception as exc:
            logger.warning("DB state sync failed for %s: %s", action.task_slug, exc)

        emit_event(
            self.session_root,
            "merge_completed" if not error else "task_merge_failed",
            action.pane_slug,
            plan_name=self.dag.name,
            task_slug=action.task_slug,
            error=error,
        )
        return actions
