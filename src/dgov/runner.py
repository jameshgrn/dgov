"""Async bridge for DagKernel — high-performance headless orchestration.

Follows Lacustrine Pillars:
- Pillar #1: Separation of Powers - Runner orchestrates; Worker implements.
- Pillar #9: Hot-Path - Zero-latency async signaling, no polling or pipes.
- Pillar #10: Fail-Closed - Graceful shutdown leaves no dangling state.
"""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
from dgov.dispatch_run import (
    DispatchRun,
    DispatchRunState,
    _dispatch_run_from_row_dict,
    derive_drift_evidence,
)
from dgov.event_types import (
    DgovEvent,
    EvtTaskDispatched,
    GovernorResumed,
    IterationFork,
    MergeCompleted,
    ReviewFail,
    ReviewPass,
    RunStart,
    SelfReviewAutoPassed,
    SelfReviewError,
    SelfReviewFixStarted,
    SelfReviewPassed,
    SelfReviewRejected,
    SettlementPhaseCompleted,
    SettlementPhaseStarted,
    SettlementRetry,
    ShutdownRequested,
    TaskAbandoned,
    TaskDone,
    TaskFailed,
    TaskMergeFailed,
    deserialize_event,
)
from dgov.kernel import DagKernel
from dgov.live_state import latest_run_start_ids
from dgov.persistence import (
    emit_event,
    record_runtime_artifact,
    update_runtime_artifact_state,
)
from dgov.persistence.dispatch_runs import (
    get_dispatch_run,
    get_dispatch_runs_for_unit,
    save_dispatch_run,
)
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.prompt_builder import PromptBuilder, build_baseline_diag_note, load_review_sop_blocks
from dgov.semantic_settlement import summarize_evidence
from dgov.settlement import ReviewResult, review_sandbox
from dgov.settlement_flow import (
    IntegrationRiskRecord,
    RiskLevel,
    SettlementFlow,
)
from dgov.types import WorkerExit, Worktree
from dgov.workers.headless import run_headless_worker
from dgov.worktree import (
    create_worktree,
    prepare_worktree,
    remove_worktree,
)

logger = logging.getLogger(__name__)
_TEST_FAILURE_COMMAND_RE = re.compile(r"^Test failure from `(?P<command>[^`]+)`:", re.MULTILINE)
DispatchCoroutine = Coroutine[Any, Any, list[DagAction]]
DispatchJob = tuple[str, DispatchCoroutine]
KernelActionHandler = Callable[
    [Any, list[DispatchJob], list[DagAction]],
    Awaitable[bool | None],
]


def _normalize_scope_path(path: str) -> str:
    return path.strip().lstrip("./").rstrip("/")


def _verify_test_targets(task: DagTaskSpec, test_dir: str) -> tuple[str, ...]:
    test_root = _normalize_scope_path(test_dir)
    if not test_root:
        return ()
    claimed = (
        *task.files.create,
        *task.files.edit,
        *task.files.touch,
        *task.files.read,
    )
    return tuple(
        dict.fromkeys(
            norm
            for path in claimed
            if (norm := _normalize_scope_path(path))
            and (norm == test_root or norm.startswith(f"{test_root}/"))
        )
    )


def _test_failure_command(error: str) -> str | None:
    match = _TEST_FAILURE_COMMAND_RE.search(error)
    if match is None:
        return None
    return match.group("command").strip() or None


def _summarize_evidence(risk_record: IntegrationRiskRecord) -> str:
    return summarize_evidence(risk_record.overlap_evidence)


@dataclass
class TaskContext:
    """Per-task runtime state tracked by EventDagRunner."""

    pane_slug: str | None = None
    attempts: int = 0
    error: str | None = None
    start_time: float | None = None
    duration: float | None = None
    worktree: Worktree | None = None
    worker_task: asyncio.Task[None] | None = None
    rejected_worktree: Worktree | None = None
    call_count: int = 0
    fork_depth: int = 0
    review_file_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    current_dispatch_run_id: str | None = None
    retried_from_dispatch_run_id: str | None = None
    forked_from_dispatch_run_id: str | None = None


@dataclass(frozen=True)
class _RunLoopStep:
    actions: list[DagAction]
    final: dict[str, str] | None = None
    stop: bool = False


class EventDagRunner:
    """Async DAG runner — pure event-driven dispatch."""

    def __init__(
        self,
        dag: DagDefinition,
        session_root: str = ".",
        on_event: Callable[[str, str, object], None] | None = None,
        restart: bool = False,
        continue_failed: bool = False,
        dispatched_by: str = "watermaster:unknown",
    ) -> None:
        from dgov.config import load_project_config
        from dgov.persistence import reset_plan_state

        self.dag = dag
        self.session_root = session_root
        self.on_event = on_event
        self._dispatched_by = dispatched_by
        self.project_config = load_project_config(session_root)
        self.deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
        self._tasks: dict[str, TaskContext] = {}

        self.task_files, self.task_read_files = self._build_file_claims(dag)
        self.kernel = self._init_kernel(dag)
        self._prompts = self._build_prompts(session_root, dag)
        self._init_async_and_settlement_state(session_root, dag)

        if restart:
            reset_plan_state(session_root, dag.name)
        else:
            self._run_recovery_pipeline(continue_failed=continue_failed)

    def _build_file_claims(
        self, dag: DagDefinition
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
        """Build file claims and read-file claims from dag tasks."""
        task_files = {
            slug: tuple(
                dict.fromkeys(t.files.create + t.files.edit + t.files.delete + t.files.touch)
            )
            for slug, t in dag.tasks.items()
        }
        task_read_files = {slug: tuple(t.files.read) for slug, t in dag.tasks.items()}
        return task_files, task_read_files

    def _init_kernel(self, dag: DagDefinition) -> DagKernel:
        """Initialize the DagKernel with deps and file claims."""
        return DagKernel(
            deps=self.deps,
            task_files=self.task_files,
            max_retries=dag.default_max_retries,
        )

    def _build_prompts(self, session_root: str, dag: DagDefinition) -> PromptBuilder:
        """Build the PromptBuilder with baseline diag and review SOP blocks."""
        return PromptBuilder(
            session_root=session_root,
            dag=dag,
            baseline_diag_note=build_baseline_diag_note(self.project_config, session_root),
            review_sop_blocks=load_review_sop_blocks(session_root),
        )

    def _init_async_and_settlement_state(self, session_root: str, dag: DagDefinition) -> None:
        """Initialize async primitives, shutdown handling, and settlement flow."""
        self._pending_dispatches: set[str] = set()
        self._event_queue: asyncio.Queue[WorkerExit] = asyncio.Queue()
        self._settlement_semaphore = asyncio.Semaphore(1)
        self._shutdown_event = asyncio.Event()
        self._shutdown_interrupted = False
        self._settlement_flow = SettlementFlow(
            session_root=session_root,
            plan_name=dag.name,
            project_config=self.project_config,
        )

    def _ctx(self, slug: str) -> TaskContext:
        """Get or create TaskContext for a slug."""
        ctx = self._tasks.get(slug)
        if ctx is None:
            ctx = TaskContext()
            self._tasks[slug] = ctx
        return ctx

    def _pane_slug_for_task(self, slug: str) -> str:
        ctx = self._tasks.get(slug)
        return ctx.pane_slug if ctx and ctx.pane_slug else ""

    def _merge_action_with_context(self, action: MergeTask) -> MergeTask:
        pane_slug = action.pane_slug or self._pane_slug_for_task(action.task_slug)
        if pane_slug == action.pane_slug:
            return action
        return MergeTask(action.task_slug, pane_slug, action.file_claims)

    @property
    def task_errors(self) -> dict[str, str]:
        """Reconstruct errors dict for CLI consumption."""
        return {slug: ctx.error for slug, ctx in self._tasks.items() if ctx.error}

    @property
    def task_durations(self) -> dict[str, float]:
        """Reconstruct durations dict for CLI consumption."""
        return {
            slug: ctx.duration for slug, ctx in self._tasks.items() if ctx.duration is not None
        }

    @property
    def token_usage(self) -> dict[str, tuple[int, int]]:
        """Return per-task token usage as prompt/completion pairs."""
        return {
            slug: (ctx.prompt_tokens, ctx.completion_tokens)
            for slug, ctx in self._tasks.items()
            if ctx.prompt_tokens or ctx.completion_tokens
        }

    def _run_recovery_pipeline(self, continue_failed: bool = False) -> None:
        """Orchestrate recovery phases: seed → rehydrate → cleanup → resume.

        Phases are ordered for deterministic fail-closed behavior:
        1. Seed deployed state from deploy log (static baseline)
        2. Rehydrate from latest-run events (replay what happened)
        3. Cleanup orphaned ACTIVE tasks (mark ABANDONED)
        4. Resume failed tasks if requested (move to PENDING)
        """
        self._phase_seed_deployed()
        self._phase_rehydrate()
        self._phase_cleanup_orphans()
        if continue_failed:
            self._phase_resume_failed()

    def _phase_seed_deployed(self) -> None:
        """Phase 1: Mark already-deployed units as MERGED before replaying latest-run events."""
        from dgov import deploy_log

        deployed_units = {
            record.unit for record in deploy_log.read(self.session_root, self.dag.name)
        }
        for slug in deployed_units:
            if slug in self.kernel.task_states:
                self.kernel.task_states[slug] = TaskState.MERGED

    def _phase_cleanup_orphans(self) -> None:
        """Phase 3: Abandon any ACTIVE tasks left over from a crashed prior run.

        After rehydration, ACTIVE tasks have no live worker — they are orphans.
        Mark them ABANDONED so --continue can retry them, and a bare run doesn't
        deadlock waiting for workers that will never finish.
        """
        for slug, state in list(self.kernel.task_states.items()):
            if state == TaskState.ACTIVE:
                self._abandon_orphaned_task(slug)

    def _abandon_orphaned_task(self, slug: str) -> None:
        """Mark a single orphaned ACTIVE task as ABANDONED. Extracted for testability."""
        logger.warning("Orphaned ACTIVE task after rehydration: %s — marking ABANDONED", slug)
        self.kernel.handle(TaskWaitDone(slug, "cleanup", TaskState.ABANDONED))
        update_runtime_artifact_state(
            self.session_root,
            slug,
            TaskState.ABANDONED.value,
            force=True,
        )
        emit_event(
            self.session_root,
            TaskAbandoned(
                pane="cleanup",
                plan_name=self.dag.name,
                task_slug=slug,
            ),
        )

    def _phase_resume_failed(self) -> None:
        """Phase 4: Move all FAILED/ABANDONED/TIMED_OUT/SKIPPED tasks back to PENDING for retry.

        Only executes when continue_failed=True. Preserves the prior state for logging
        and emits governor-resumed events for auditability.
        """
        logger.info("Resuming failed tasks")
        for slug, state in list(self.kernel.task_states.items()):
            if state in (
                TaskState.FAILED,
                TaskState.ABANDONED,
                TaskState.TIMED_OUT,
                TaskState.SKIPPED,
            ):
                self._resume_single_task(slug, state)

    def _resume_single_task(self, slug: str, prior_state: TaskState) -> None:
        """Resume a single failed/abandoned task. Extracted for testability."""
        logger.info("Resuming task: %s (prior state: %s)", slug, prior_state)
        ctx = self._ctx(slug)
        ctx.attempts += 1
        ctx.retried_from_dispatch_run_id = ctx.current_dispatch_run_id
        self.kernel.handle(TaskGovernorResumed(slug, GovernorAction.RETRY))
        emit_event(
            self.session_root,
            GovernorResumed(
                pane="runner",
                plan_name=self.dag.name,
                task_slug=slug,
                action=GovernorAction.RETRY.value,
            ),
        )

    def _phase_rehydrate(self) -> None:
        """Phase 2: Replay latest-run events to restore kernel state.

        Scopes replay to events after the most recent run_start marker for
        deterministic recovery. Preserves timeout detection from task_failed
        events and governor-resume event handling for state machine consistency.
        """
        from dgov.persistence import read_events

        events = read_events(self.session_root, plan_name=self.dag.name)
        run_start_id = latest_run_start_ids(events).get(self.dag.name, 0)
        for ev in events:
            if int(ev.get("id", 0)) <= run_start_id:
                continue
            typed_event = deserialize_event(ev)
            self._apply_rehydrate_event(typed_event)
        self._rehydrate_dispatch_run_contexts()

    def _rehydrate_dispatch_run_contexts(self) -> None:
        """Restore latest DispatchRun ids for retry/fork lineage after process restart."""
        for slug in self.dag.tasks:
            rows = get_dispatch_runs_for_unit(self.session_root, slug)
            if rows:
                self._ctx(slug).current_dispatch_run_id = rows[-1]["id"]

    def _apply_rehydrate_event(self, event: DgovEvent) -> None:
        """Apply a single event during rehydration. Extracted for testability."""
        task_slug = getattr(event, "task_slug", None)
        pane = getattr(event, "pane", "")

        if not task_slug or task_slug not in self.kernel.task_states:
            return
        if pane:
            self._ctx(task_slug).pane_slug = pane

        status = self._rehydrated_wait_status(event)
        if status is not None:
            self.kernel.handle(TaskWaitDone(task_slug, pane, status))
            return

        review_result = self._rehydrated_review_result(event)
        if review_result is not None:
            passed, commit_count = review_result
            self.kernel.handle(
                TaskReviewDone(
                    task_slug,
                    passed=passed,
                    verdict="rehydrated",
                    commit_count=commit_count,
                )
            )
            return

        merge_result = self._rehydrated_merge_result(event)
        if merge_result is not None:
            (merge_error,) = merge_result
            self.kernel.handle(TaskMergeDone(task_slug, error=merge_error))
            return

        if isinstance(event, EvtTaskDispatched):
            self.kernel.handle(TaskDispatched(task_slug, pane))
        elif isinstance(event, GovernorResumed):
            self._apply_rehydrated_governor_resume(task_slug, event)

    def _rehydrated_wait_status(self, event: DgovEvent) -> TaskState | None:
        if isinstance(event, TaskDone):
            return TaskState.DONE
        if isinstance(event, TaskAbandoned):
            return TaskState.ABANDONED
        if isinstance(event, TaskFailed):
            error = (event.error or "").lower()
            return TaskState.TIMED_OUT if "timeout" in error else TaskState.FAILED
        return None

    def _rehydrated_review_result(self, event: DgovEvent) -> tuple[bool, int] | None:
        if isinstance(event, ReviewPass):
            return True, 1
        if isinstance(event, ReviewFail):
            return False, 0
        return None

    def _rehydrated_merge_result(self, event: DgovEvent) -> tuple[str | None] | None:
        if isinstance(event, MergeCompleted):
            return (None,)
        if isinstance(event, TaskMergeFailed):
            return (event.error or "unknown error",)
        return None

    def _apply_rehydrated_governor_resume(
        self,
        task_slug: str,
        event: GovernorResumed,
    ) -> None:
        action_str = event.action or None
        if not action_str:
            return
        try:
            action = GovernorAction(action_str)
        except ValueError:
            return

        self.kernel.handle(TaskGovernorResumed(task_slug, action))
        if action == GovernorAction.RETRY:
            self._ctx(task_slug).attempts += 1

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
            ShutdownRequested(
                pane="runner",
                plan_name=self.dag.name,
                reason="signal",
            ),
        )

    async def _cleanup(self) -> None:
        """Cleanup all resources — worktrees, executor, connections (Pillar #3, #10)."""
        active_workers = self._active_worker_tasks()
        active_wts = self._active_worktrees()
        rejected_wts = self._rejected_worktrees()
        logger.info(
            "Cleaning up %d worker tasks, %d worktrees",
            len(active_workers),
            len(active_wts) + len(rejected_wts),
        )

        await self._cancel_active_workers(active_workers)
        for ctx in self._tasks.values():
            ctx.worker_task = None
        self._pending_dispatches.clear()

        to_clean = list(active_wts)
        if self._shutdown_event.is_set():
            to_clean += rejected_wts

        await self._remove_worktrees(to_clean)
        for ctx in self._tasks.values():
            ctx.worktree = None
            if self._shutdown_event.is_set():
                ctx.rejected_worktree = None

        logger.info("Cleanup complete")

    def _active_worker_tasks(self) -> list[tuple[str, asyncio.Task]]:
        return [(slug, ctx.worker_task) for slug, ctx in self._tasks.items() if ctx.worker_task]

    def _active_worktrees(self) -> list[Worktree]:
        return [ctx.worktree for ctx in self._tasks.values() if ctx.worktree]

    def _rejected_worktrees(self) -> list[Worktree]:
        return [ctx.rejected_worktree for ctx in self._tasks.values() if ctx.rejected_worktree]

    async def _cancel_active_workers(self, active_workers: list[tuple[str, asyncio.Task]]) -> None:
        for task_slug, atask in active_workers:
            if not atask.done():
                atask.cancel()
                logger.debug("Cancelled worker task: %s", task_slug)

        if active_workers:
            await asyncio.gather(*[atask for _, atask in active_workers], return_exceptions=True)

    async def _remove_worktrees(self, worktrees: list[Worktree]) -> None:
        for wt in worktrees:
            try:
                await asyncio.to_thread(remove_worktree, self.session_root, wt)
            except Exception as exc:
                logger.warning("Failed to remove worktree: %s", exc)

    def _abandon_active_tasks_for_shutdown(self) -> list[DagAction]:
        actions: list[DagAction] = []
        for task_slug, state in list(self.kernel.task_states.items()):
            if state != TaskState.ACTIVE:
                continue
            self._shutdown_interrupted = True
            pane_slug = self._pane_slug_for_task(task_slug)
            logger.warning("Task %s interrupted by operator — marking ABANDONED", task_slug)
            emit_event(
                self.session_root,
                TaskAbandoned(
                    pane=pane_slug or "runner",
                    plan_name=self.dag.name,
                    task_slug=task_slug,
                    reason="shutdown",
                ),
            )
            actions.extend(
                self.kernel.handle(TaskWaitDone(task_slug, pane_slug, TaskState.ABANDONED))
            )
        return actions

    async def run(self) -> dict[str, str]:
        """Execute DAG with high-performance async loop."""
        self._setup_signal_handlers()
        await self._check_model_env()

        # Emit a run-start marker so dgov plan review can scope events to
        # the current invocation. Events persist across runs unless --restart
        # is passed, so review needs an explicit lower bound.
        emit_event(
            self.session_root,
            RunStart(
                pane=f"run-{self.dag.name}",
                plan_name=self.dag.name,
            ),
        )

        try:
            result = await self._run_loop()
            if self._shutdown_interrupted:
                raise KeyboardInterrupt
            return result
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

    async def _gather_dispatch_results(self, dispatch_coros: list[DispatchJob]) -> list[DagAction]:
        """Await dispatch/merge coros, convert exceptions to FAIL actions."""
        next_actions: list[DagAction] = []
        coros: list[DispatchCoroutine] = [c for _, c in dispatch_coros]
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
        dispatch_coros: list[DispatchJob] = []
        next_actions: list[DagAction] = []

        for action in actions:
            final = await self._queue_kernel_action(action, dispatch_coros, next_actions)
            if final is not None:
                return [], self._task_state_snapshot()

        if dispatch_coros:
            next_actions.extend(await self._gather_dispatch_results(dispatch_coros))
        return next_actions, None

    async def _queue_kernel_action(
        self,
        action: DagAction,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        handler = self._kernel_action_handlers().get(type(action))
        if handler is None:
            return None
        return await handler(action, dispatch_coros, next_actions)

    def _kernel_action_handlers(self) -> dict[type[DagAction], KernelActionHandler]:
        return {
            DispatchTask: self._queue_dispatch_action,
            MergeTask: self._queue_merge_action,
            ReviewTask: self._queue_review_kernel_action,
            CleanupTask: self._queue_cleanup_action,
            InterruptGovernor: self._queue_interrupt_action,
            DagDone: self._queue_done_action,
        }

    async def _queue_dispatch_action(
        self,
        action: DispatchTask,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        dispatch_coros.append((action.task_slug, self._dispatch(action)))
        return None

    async def _queue_merge_action(
        self,
        action: MergeTask,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        dispatch_coros.append((action.task_slug, self._merge(action)))
        return None

    async def _queue_review_kernel_action(
        self,
        action: ReviewTask,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        self._queue_review_action(action, dispatch_coros, next_actions)
        return None

    async def _queue_cleanup_action(
        self,
        action: CleanupTask,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        next_actions.extend(await self._cleanup_task(action))
        return None

    async def _queue_interrupt_action(
        self,
        action: InterruptGovernor,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        next_actions.extend(self._handle_interrupt(action))
        return None

    async def _queue_done_action(
        self,
        action: DagDone,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> bool | None:
        return True

    def _queue_review_action(
        self,
        action: ReviewTask,
        dispatch_coros: list[DispatchJob],
        next_actions: list[DagAction],
    ) -> None:
        structural = self._run_structural_review(action)
        if structural is not None:
            next_actions.extend(structural)
            return
        dispatch_coros.append((action.task_slug, self._run_self_review_gate(action)))

    async def _cleanup_task(self, action: CleanupTask) -> list[DagAction]:
        """Remove worktree for a task (terminal failure/skip)."""
        ctx = self._tasks.get(action.task_slug)
        wt = ctx.worktree if ctx else None
        if ctx:
            ctx.worktree = None
        if wt:
            try:
                await asyncio.to_thread(remove_worktree, self.session_root, wt)
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
                update_runtime_artifact_state(
                    self.session_root, action.task_slug, kernel_state.value, force=True
                )
            except Exception as exc:
                logger.warning("DB state sync failed for %s: %s", action.task_slug, exc)
        return []

    _ITERATION_EXHAUSTED_MARKER = "Exceeded max iterations"

    def _handle_worker_exit(self, exit_event: WorkerExit) -> list[DagAction]:
        """Convert a worker exit into kernel actions, recording errors and emitting events."""
        self._pending_dispatches.discard(exit_event.task_slug)
        ctx = self._ctx(exit_event.task_slug)
        self._record_worker_exit(ctx, exit_event)

        task = self.dag.tasks[exit_event.task_slug]
        will_fork = self._should_fork_after_exit(exit_event, ctx, task)
        self._record_terminal_dispatch_run(
            exit_event=exit_event,
            ctx=ctx,
            state=self._dispatch_terminal_state(exit_event, will_fork=will_fork),
        )
        if will_fork:
            self._start_iteration_fork(exit_event, ctx, task)
            return []

        status = TaskState.DONE if exit_event.exit_code == 0 else TaskState.FAILED
        duration = self._finish_task_duration(ctx)
        actions = self.kernel.handle(
            TaskWaitDone(exit_event.task_slug, exit_event.pane_slug, status)
        )
        self._emit_worker_terminal_event(exit_event, ctx, status, duration)
        return actions

    def _record_worker_exit(self, ctx: TaskContext, exit_event: WorkerExit) -> None:
        ctx.worker_task = None
        ctx.prompt_tokens += exit_event.prompt_tokens
        ctx.completion_tokens += exit_event.completion_tokens
        if exit_event.last_error:
            ctx.error = exit_event.last_error

    def _should_fork_after_exit(
        self,
        exit_event: WorkerExit,
        ctx: TaskContext,
        task: DagTaskSpec,
    ) -> bool:
        return (
            exit_event.exit_code != 0
            and self._ITERATION_EXHAUSTED_MARKER in (exit_event.last_error or "")
            and ctx.fork_depth < task.max_fork_depth
            and ctx.worktree is not None
        )

    def _start_iteration_fork(
        self,
        exit_event: WorkerExit,
        ctx: TaskContext,
        task: DagTaskSpec,
    ) -> None:
        ctx.fork_depth += 1
        ctx.forked_from_dispatch_run_id = ctx.current_dispatch_run_id
        ctx.call_count = 0
        ctx.start_time = time.time()
        self._pending_dispatches.add(exit_event.task_slug)
        logger.info(
            "Task %s exhausted iterations — forking with clean context (depth %d/%d)",
            exit_event.task_slug,
            ctx.fork_depth,
            task.max_fork_depth,
        )
        emit_event(
            self.session_root,
            IterationFork(
                pane=exit_event.pane_slug,
                plan_name=self.dag.name,
                task_slug=exit_event.task_slug,
                fork_depth=ctx.fork_depth,
            ),
        )
        if ctx.worktree is not None:
            ctx.worker_task = asyncio.create_task(
                self._fork_worker(exit_event.task_slug, ctx.worktree, exit_event.pane_slug)
            )

    def _finish_task_duration(self, ctx: TaskContext) -> float | None:
        start_time = ctx.start_time
        ctx.start_time = None
        duration = round(time.time() - start_time, 2) if start_time else None
        if duration is not None:
            ctx.duration = duration
        return duration

    def _emit_worker_terminal_event(
        self,
        exit_event: WorkerExit,
        ctx: TaskContext,
        status: TaskState,
        duration: float | None,
    ) -> None:
        if status == TaskState.DONE:
            emit_event(
                self.session_root,
                TaskDone(
                    pane=exit_event.pane_slug,
                    plan_name=self.dag.name,
                    task_slug=exit_event.task_slug,
                    error=None,
                    duration=duration,
                    prompt_tokens=ctx.prompt_tokens or None,
                    completion_tokens=ctx.completion_tokens or None,
                ),
            )
            return
        emit_event(
            self.session_root,
            TaskFailed(
                pane=exit_event.pane_slug,
                plan_name=self.dag.name,
                task_slug=exit_event.task_slug,
                error=exit_event.last_error or "",
                duration=duration,
                prompt_tokens=ctx.prompt_tokens or None,
                completion_tokens=ctx.completion_tokens or None,
            ),
        )

    async def _get_worktree_diff(self, wt: Worktree) -> str:
        """Get the diff of uncommitted changes in a worktree."""
        import subprocess as sp

        try:
            diff_result = await asyncio.to_thread(
                lambda: sp.run(
                    ["git", "diff", "HEAD"],
                    cwd=wt.path,
                    capture_output=True,
                    text=True,
                    timeout=15,
                ),
            )
            return diff_result.stdout or ""
        except Exception:
            return ""

    async def _fork_worker(
        self,
        task_slug: str,
        wt: Worktree,
        pane_slug: str,
    ) -> None:
        """Fork a fresh worker with clean context in the existing worktree.

        The forked worker inherits the worktree state (uncommitted changes)
        but gets a distilled prompt with the diff of work done so far.

        On any failure, pushes a failed WorkerExit so _run_loop never hangs.
        """
        task = self.dag.tasks[task_slug]
        ctx = self._ctx(task_slug)
        fork_pane = f"{pane_slug}-fork-{ctx.fork_depth}"
        push_exit = self._fork_worker_exit_callback()

        try:
            self._mint_dispatch_run(
                task_slug=task_slug,
                wt=wt,
                agent=self._resolved_task_agent(task),
                ctx=ctx,
            )
            await self._run_forked_worker(task_slug, wt, fork_pane, task, ctx, push_exit)
        except TimeoutError:
            logger.error("Forked worker %s timed out after %ds", task_slug, task.timeout_s)
            push_exit(task_slug, fork_pane, 1, f"Fork timed out after {task.timeout_s}s", 0, 0)
        except Exception as exc:
            logger.error("Fork failed for %s: %s", task_slug, exc)
            push_exit(task_slug, fork_pane, 1, f"Fork failed: {exc}", 0, 0)

    def _fork_worker_exit_callback(self) -> Callable[[str, str, int, str, int, int], None]:
        def _push_exit(
            slug: str,
            pane: str,
            code: int,
            err: str = "",
            prompt_tokens: int = 0,
            completion_tokens: int = 0,
        ) -> None:
            self._push_worker_exit(
                task_slug=slug,
                pane_slug=pane,
                exit_code=code,
                output_dir="",
                last_error=err,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return _push_exit

    async def _run_forked_worker(
        self,
        task_slug: str,
        wt: Worktree,
        fork_pane: str,
        task: DagTaskSpec,
        ctx: TaskContext,
        push_exit: Callable[[str, str, int, str, int, int], None],
    ) -> None:
        diff_text = await self._get_worktree_diff(wt) or "(no diff available)"
        forked_task = self._forked_task(task, ctx.fork_depth, diff_text)
        await asyncio.wait_for(
            run_headless_worker(
                self.session_root,
                self.dag.name,
                task_slug,
                fork_pane,
                wt.path,
                forked_task,
                self._retry_scope(task_slug, task),
                push_exit,
                on_event=self._make_counted_on_event(task_slug),
            ),
            timeout=float(task.timeout_s) if task.timeout_s > 0 else None,
        )

    def _forked_task(self, task: DagTaskSpec, fork_depth: int, diff_text: str) -> DagTaskSpec:
        handoff_prompt = PromptBuilder.fork_handoff_prompt(task, diff_text)
        return task.model_copy(
            update={
                "summary": f"[fork-{fork_depth}] {task.summary}",
                "prompt": handoff_prompt,
            }
        )

    # ---- Self-review (clean-context semantic review) ----

    async def _run_self_review_gate(self, action: ReviewTask) -> list[DagAction]:
        """Async self-review gate — runs after structural review passes."""
        task = self.dag.tasks[action.task_slug]
        ctx = self._ctx(action.task_slug)
        wt = ctx.worktree
        if not wt:
            return self._pass_skipped_self_review(action, ctx)

        self._emit_structural_review_pass(action)

        # Self-review is advisory — any failure auto-passes to settlement
        try:
            await self._complete_self_review_cycle(action, wt, task)
        except Exception as exc:
            self._emit_self_review_error(action, exc)

        return self._pass_completed_self_review(action, ctx)

    def _pass_skipped_self_review(
        self,
        action: ReviewTask,
        ctx: TaskContext,
    ) -> list[DagAction]:
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=True,
                verdict="self_review_skipped",
                commit_count=ctx.review_file_count,
            )
        )

    def _emit_structural_review_pass(self, action: ReviewTask) -> None:
        emit_event(
            self.session_root,
            ReviewPass(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict="structural_pass",
            ),
        )

    async def _complete_self_review_cycle(
        self,
        action: ReviewTask,
        wt: Worktree,
        task: DagTaskSpec,
    ) -> None:
        passed, findings = await self._run_self_review(action.task_slug, wt, action.pane_slug)
        if passed:
            self._emit_self_review_passed(action)
            return

        self._emit_self_review_rejected(action, findings or "")
        await self._relaunch_worker_with_findings(action.task_slug, wt, findings or "", task)
        passed2, findings2 = await self._run_self_review(action.task_slug, wt, action.pane_slug)
        if passed2:
            self._emit_self_review_passed(action)
        else:
            self._emit_self_review_auto_passed(action, findings2)

    def _emit_self_review_passed(self, action: ReviewTask) -> None:
        emit_event(
            self.session_root,
            SelfReviewPassed(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
            ),
        )

    def _emit_self_review_rejected(self, action: ReviewTask, findings: str) -> None:
        emit_event(
            self.session_root,
            SelfReviewRejected(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                findings=findings,
            ),
        )

    def _emit_self_review_auto_passed(
        self,
        action: ReviewTask,
        findings: str | None,
    ) -> None:
        emit_event(
            self.session_root,
            SelfReviewAutoPassed(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                findings=findings,
            ),
        )

    def _emit_self_review_error(self, action: ReviewTask, exc: Exception) -> None:
        logger.warning("Self-review failed for %s, auto-passing: %s", action.task_slug, exc)
        emit_event(
            self.session_root,
            SelfReviewError(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                error=str(exc),
            ),
        )

    def _pass_completed_self_review(
        self,
        action: ReviewTask,
        ctx: TaskContext,
    ) -> list[DagAction]:
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=True,
                verdict="self_review_complete",
                commit_count=ctx.review_file_count,
            )
        )

    def _self_review_capture(self, task_slug: str, captured: list[str]) -> Callable:
        def _capture(slug: str, log_type: str, content: object) -> None:
            if log_type == "done" and content:
                captured.append(str(content))
            if self.on_event is not None:
                self.on_event(f"{task_slug}/self-review", log_type, content)

        return _capture

    def _self_review_task_spec(
        self,
        task_slug: str,
        task: DagTaskSpec,
        prompt: str,
    ) -> DagTaskSpec:
        return DagTaskSpec(
            slug=f"{task_slug}-self-review",
            summary=f"Semantic review of {task_slug}",
            prompt=prompt,
            role="reviewer",
            agent=task.agent,
            timeout_s=120,
        )

    def _parse_self_review_output(self, output: str) -> tuple[bool, str | None]:
        import json

        try:
            verdict = json.loads(output)
        except (json.JSONDecodeError, AttributeError):
            return self._parse_text_self_review_output(output)
        if verdict.get("approved", True):
            return True, None
        issues = verdict.get("issues", [])
        return False, "\n".join(f"- {issue}" for issue in issues)

    def _parse_text_self_review_output(self, output: str) -> tuple[bool, str | None]:
        lower = output.lower()
        if any(w in lower for w in ("no issues", "looks good", "approved", "lgtm")):
            return True, None
        if any(w in lower for w in ("issue", "bug", "error", "incorrect", "wrong", "missing")):
            return False, output
        return True, None

    async def _run_self_review(
        self,
        task_slug: str,
        wt: Worktree,
        pane_slug: str,
    ) -> tuple[bool, str | None]:
        """Spawn a clean-context reviewer on the diff. Returns (passed, findings)."""
        task = self.dag.tasks[task_slug]

        diff_text = await self._get_worktree_diff(wt)
        if not diff_text.strip():
            return True, None

        review_prompt = self._prompts.self_review_prompt(diff_text)
        captured: list[str] = []
        reviewer_task = self._self_review_task_spec(task_slug, task, review_prompt)
        reviewer_scope: dict[str, object] = {"task_slug": f"{task_slug}-self-review"}

        await asyncio.wait_for(
            run_headless_worker(
                self.session_root,
                self.dag.name,
                f"{task_slug}-self-review",
                f"{pane_slug}-self-review",
                wt.path,
                reviewer_task,
                reviewer_scope,
                self._noop_retry_exit,
                on_event=self._self_review_capture(task_slug, captured),
            ),
            timeout=120.0,
        )

        if not captured:
            return True, None
        return self._parse_self_review_output(captured[-1])

    def _self_review_fix_task_spec(self, task: DagTaskSpec, findings: str) -> DagTaskSpec:
        fix_prompt = (
            "A semantic review of your changes found the following issues:\n\n"
            f"{findings}\n\n"
            "Fix these issues in the current worktree, then call done.\n"
            "Use git_diff to see your current changes.\n\n"
            f"ORIGINAL TASK:\n{task.prompt or ''}"
        )
        return task.model_copy(
            update={
                "summary": f"[review-fix] {task.summary}",
                "prompt": fix_prompt,
            }
        )

    def _self_review_fix_pane_slug(self, task_slug: str) -> str:
        return self._ctx(task_slug).pane_slug or ""

    def _emit_self_review_fix_started(self, task_slug: str) -> None:
        emit_event(
            self.session_root,
            SelfReviewFixStarted(
                pane=self._self_review_fix_pane_slug(task_slug),
                plan_name=self.dag.name,
                task_slug=task_slug,
            ),
        )

    async def _relaunch_worker_with_findings(
        self,
        task_slug: str,
        wt: Worktree,
        findings: str,
        task: DagTaskSpec,
    ) -> None:
        """Re-launch worker in same worktree with self-review findings."""
        fix_task = self._self_review_fix_task_spec(task, findings)
        fix_scope = self._retry_scope(task_slug, task)
        pane_slug = self._self_review_fix_pane_slug(task_slug)

        self._emit_self_review_fix_started(task_slug)

        await asyncio.wait_for(
            run_headless_worker(
                self.session_root,
                self.dag.name,
                task_slug,
                f"{pane_slug}-review-fix",
                wt.path,
                fix_task,
                fix_scope,
                self._noop_retry_exit,
                on_event=self.on_event,
            ),
            timeout=float(task.timeout_s) if task.timeout_s > 0 else None,
        )

    async def _run_loop(self) -> dict[str, str]:
        """Main event loop — separated for graceful shutdown handling."""
        actions = self.kernel.start()
        while True:
            step = await self._run_loop_step(actions)
            if step.final is not None:
                return step.final
            if step.stop:
                break
            actions = step.actions
        return self._task_state_snapshot()

    async def _run_loop_step(self, actions: list[DagAction]) -> _RunLoopStep:
        if actions:
            return await self._run_actions_step(actions)
        shutdown_step = self._shutdown_loop_step()
        if shutdown_step is not None:
            return shutdown_step
        if self.kernel.done:
            return _RunLoopStep(actions=[], stop=True)
        return await self._event_loop_step()

    async def _run_actions_step(self, actions: list[DagAction]) -> _RunLoopStep:
        next_actions, final = await self._process_actions(actions)
        if final is not None:
            return _RunLoopStep(actions=[], final=final)
        return _RunLoopStep(actions=next_actions)

    def _shutdown_loop_step(self) -> _RunLoopStep | None:
        if not self._shutdown_event.is_set():
            return None
        actions = self._handle_loop_shutdown()
        return _RunLoopStep(actions=actions, stop=not actions)

    async def _event_loop_step(self) -> _RunLoopStep:
        exit_event = await self._wait_for_loop_event()
        if exit_event is not None:
            return _RunLoopStep(actions=self._handle_worker_exit(exit_event))
        return _RunLoopStep(actions=[], stop=self._loop_is_idle_done())

    def _handle_loop_shutdown(self) -> list[DagAction]:
        actions = self._abandon_active_tasks_for_shutdown()
        if not actions:
            self._shutdown_interrupted = True
        return actions

    def _loop_is_idle_done(self) -> bool:
        return not self._pending_dispatches and self.kernel.done

    async def _wait_for_loop_event(self) -> WorkerExit | None:
        queue_get = asyncio.create_task(self._event_queue.get())
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        try:
            done, pending = await asyncio.wait(
                {queue_get, shutdown_wait},
                timeout=5.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if queue_get in done:
                return queue_get.result()
            return None
        finally:
            if not queue_get.done():
                queue_get.cancel()
            if not shutdown_wait.done():
                shutdown_wait.cancel()
            await asyncio.gather(queue_get, shutdown_wait, return_exceptions=True)

    def _run_structural_review(self, action: ReviewTask) -> list[DagAction] | None:
        """Structural review gate (scope check). Returns None if self-review needed."""
        task = self.dag.tasks[action.task_slug]

        if task.role in ("researcher", "reviewer"):
            return self._pass_read_only_review(action)

        ctx = self._tasks.get(action.task_slug)
        wt = ctx.worktree if ctx else None
        if not wt:
            return self._fail_missing_worktree_review(action)

        review_result = self._review_sandbox_for_action(action, wt)
        if not review_result.passed:
            return self._fail_structural_review(action, review_result)

        if task.self_review and task.role == "worker":
            ctx = self._ctx(action.task_slug)
            ctx.review_file_count = len(review_result.actual_files)
            return None

        return self._pass_structural_review(action, review_result)

    def _pass_read_only_review(self, action: ReviewTask) -> list[DagAction]:
        emit_event(
            self.session_root,
            ReviewPass(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict="read_only",
            ),
        )
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=True,
                verdict="read_only",
                commit_count=0,
            )
        )

    def _fail_missing_worktree_review(self, action: ReviewTask) -> list[DagAction]:
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=False,
                verdict="worktree_missing",
                commit_count=0,
            )
        )

    def _review_sandbox_for_action(self, action: ReviewTask, wt: Worktree) -> ReviewResult:
        return review_sandbox(
            wt.path,
            claimed_files=self.task_files.get(action.task_slug),
            read_files=self.task_read_files.get(action.task_slug, ()),
            project_root=self.session_root,
            task_slug=action.task_slug,
            pane_slug=action.pane_slug,
            scope_ignore_files=self.project_config.scope_ignore_files,
        )

    def _fail_structural_review(
        self,
        action: ReviewTask,
        review_result: ReviewResult,
    ) -> list[DagAction]:
        emit_event(
            self.session_root,
            ReviewFail(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict=review_result.verdict,
                error=review_result.error or "",
            ),
        )
        self._record_structural_review_error(action, review_result)
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=False,
                verdict=review_result.verdict,
                commit_count=len(review_result.actual_files),
            )
        )

    def _record_structural_review_error(
        self,
        action: ReviewTask,
        review_result: ReviewResult,
    ) -> None:
        if not review_result.error:
            return
        error_msg = f"review:{review_result.verdict} — {review_result.error}"
        if review_result.verdict == "scope_violation":
            error_msg += (
                f"\nhint: add these paths to files.edit in task"
                f" '{action.task_slug}', then recompile and re-run"
            )
        self._ctx(action.task_slug).error = error_msg

    def _pass_structural_review(
        self,
        action: ReviewTask,
        review_result: ReviewResult,
    ) -> list[DagAction]:
        emit_event(
            self.session_root,
            ReviewPass(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict=review_result.verdict,
            ),
        )
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=True,
                verdict=review_result.verdict,
                commit_count=len(review_result.actual_files),
            )
        )

    _NON_RETRYABLE_ERRORS = frozenset({
        "Agent stopped without calling 'done'",
    })

    _PROVIDER_RATE_LIMIT_MARKER = "Fireworks adaptive serverless TPM"

    def _is_non_retryable_provider_rate_limit(self, error_detail: str) -> bool:
        """Check if error is a non-retryable provider rate limit.

        Fireworks adaptive serverless TPM limits are infrastructure/provider
        throughput constraints, not worker-fixable issues. These should fail
        fast without wasting retry budget.
        """
        return self._PROVIDER_RATE_LIMIT_MARKER in error_detail

    def _abandon_interrupted_task(
        self,
        action: InterruptGovernor,
        error_detail: str,
    ) -> list[DagAction]:
        self._shutdown_interrupted = True
        ctx = self._ctx(action.task_slug)
        logger.warning(
            "Task %s interrupted during shutdown — marking ABANDONED: %s",
            action.task_slug,
            error_detail or action.reason,
        )
        self._record_terminal_dispatch_run(
            exit_event=WorkerExit(
                task_slug=action.task_slug,
                pane_slug=action.pane_slug or "runner",
                exit_code=1,
                output_dir="",
                last_error=error_detail or action.reason or "shutdown",
            ),
            ctx=ctx,
            state="abandoned",
        )
        emit_event(
            self.session_root,
            TaskAbandoned(
                pane=action.pane_slug or "runner",
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                reason="shutdown",
            ),
        )
        return self.kernel.handle(
            TaskWaitDone(action.task_slug, action.pane_slug, TaskState.ABANDONED)
        )

    def _interrupt_governor_action(
        self,
        action: InterruptGovernor,
        attempts: int,
        error_detail: str,
    ) -> GovernorAction:
        if error_detail in self._NON_RETRYABLE_ERRORS:
            logger.error("Task %s failed — non-retryable: %s", action.task_slug, error_detail)
            return GovernorAction.FAIL
        if self._is_non_retryable_provider_rate_limit(error_detail):
            logger.error(
                "Task %s failed — provider rate limit (non-retryable): %s. "
                "Use 'dgov run --continue <plan-dir>' after cooldown or model change.",
                action.task_slug,
                error_detail[:200],
            )
            return GovernorAction.FAIL
        if attempts < self.kernel.max_retries:
            ctx = self._ctx(action.task_slug)
            ctx.attempts = attempts + 1
            ctx.retried_from_dispatch_run_id = ctx.current_dispatch_run_id
            logger.info(
                "Task %s failed — retry %d/%d: %s",
                action.task_slug,
                attempts + 1,
                self.kernel.max_retries,
                error_detail or action.reason,
            )
            return GovernorAction.RETRY

        logger.error(
            "Task %s failed — max retries (%d) exceeded: %s",
            action.task_slug,
            self.kernel.max_retries,
            error_detail or action.reason,
        )
        return GovernorAction.FAIL

    def _emit_governor_resumed(
        self,
        action: InterruptGovernor,
        gov_action: GovernorAction,
    ) -> None:
        emit_event(
            self.session_root,
            GovernorResumed(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                action=gov_action.value,
            ),
        )

    def _handle_interrupt(self, action: InterruptGovernor) -> list[DagAction]:
        """Decide retry vs fail based on attempt count."""
        ctx = self._ctx(action.task_slug)
        attempts = ctx.attempts
        error_detail = ctx.error or ""

        if self._shutdown_event.is_set():
            return self._abandon_interrupted_task(action, error_detail)

        gov_action = self._interrupt_governor_action(action, attempts, error_detail)
        self._emit_governor_resumed(action, gov_action)
        return self.kernel.handle(TaskGovernorResumed(action.task_slug, gov_action))

    def _make_counted_on_event(self, task_slug: str) -> Callable[[str, str, object], None] | None:
        """Wrap on_event to count tool calls per task."""
        ctx = self._ctx(task_slug)

        def _counted(slug: str, log_type: str, content: object) -> None:
            if log_type == "call":
                ctx.call_count += 1
            if self.on_event is not None:
                self.on_event(slug, log_type, content)

        return _counted

    async def _run_with_timeout(
        self,
        task_slug: str,
        pane_slug: str,
        worktree_path: Path,
        task: DagTaskSpec,
        task_scope: Mapping[str, object],
        on_exit: Callable[[str, str, int, str, int, int], None],
        timeout_s: int,
        on_event: Callable[[str, str, object], None] | None = None,
    ) -> None:
        """Run headless worker with wall-clock timeout enforcement."""
        try:
            await asyncio.wait_for(
                run_headless_worker(
                    self.session_root,
                    self.dag.name,
                    task_slug,
                    pane_slug,
                    worktree_path,
                    task,
                    task_scope,
                    on_exit,
                    on_event=on_event if on_event is not None else self.on_event,
                ),
                timeout=float(timeout_s) if timeout_s > 0 else None,
            )
        except TimeoutError:
            logger.error("Task %s timed out after %ds", task_slug, timeout_s)
            emit_event(
                self.session_root,
                TaskFailed(
                    pane=pane_slug,
                    plan_name=self.dag.name,
                    task_slug=task_slug,
                    error=f"Wall-clock timeout after {timeout_s}s",
                ),
            )
            on_exit(task_slug, pane_slug, 1, f"Timed out after {timeout_s}s", 0, 0)

    def _upstream_units(self, task_slug: str) -> tuple[str, ...]:
        """Return the transitive dependency closure for a task."""
        seen: set[str] = set()
        stack = list(self.dag.tasks[task_slug].depends_on)
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            stack.extend(self.dag.tasks[dep].depends_on)
        return tuple(seen)

    def _base_ref_for_task(self, task_slug: str) -> str:
        """Choose the git base for a task's worktree."""
        task = self.dag.tasks[task_slug]
        if not task.depends_on:
            return "HEAD"

        from dgov import deploy_log

        records = {
            record.unit: record for record in deploy_log.read(self.session_root, self.dag.name)
        }
        upstream = self._upstream_units(task_slug)
        missing = sorted(dep for dep in upstream if dep not in records)
        if missing:
            raise RuntimeError(
                f"Cannot create worktree for '{task_slug}' because upstream deploy records are "
                f"missing for: {missing}. Fix: rerun or repair the plan state before continuing."
            )

        latest = max((records[dep] for dep in upstream), key=lambda record: record.ts)
        return latest.sha

    def _effective_sop_set_hash(self) -> str:
        """Hash the worker SOP bundle loaded for this dispatch."""
        from dgov.sop_bundler import compute_sop_set_hash, load_sops

        sops_dir = Path(self.session_root) / ".dgov" / "sops"
        try:
            effective_sops = load_sops(sops_dir)
            return compute_sop_set_hash(effective_sops) if effective_sops else ""
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("Effective SOP bundle could not be loaded at dispatch: %s", exc)
            return ""

    def _mint_dispatch_run(
        self,
        *,
        task_slug: str,
        wt: Worktree,
        agent: str,
        ctx: TaskContext,
    ) -> DispatchRun:
        effective_sop_set_hash = self._effective_sop_set_hash()
        plan_hash = self.dag.sop_set_hash or None
        retried_from = ctx.retried_from_dispatch_run_id
        forked_from = ctx.forked_from_dispatch_run_id
        dispatch_run = DispatchRun(
            from_plan_id=self.dag.name,
            unit_slug=task_slug,
            worktree_id=str(wt.path),
            branch=wt.branch,
            base_commit=wt.commit,
            agent_model=agent,
            effective_sop_set_hash=effective_sop_set_hash,
            drift_against_plan=(plan_hash is not None and plan_hash != effective_sop_set_hash),
            drift_evidence=derive_drift_evidence(
                plan_hash=plan_hash,
                effective_hash=effective_sop_set_hash,
            ),
            retried_from=retried_from,
            forked_from=forked_from,
            retry_index=ctx.attempts if retried_from is not None else 0,
            fork_depth=ctx.fork_depth if forked_from is not None else 0,
            dispatched_by=self._dispatched_by,
            dispatched_at=datetime.now(UTC),
        ).start_active()
        save_dispatch_run(self.session_root, dispatch_run)
        ctx.current_dispatch_run_id = dispatch_run.id
        ctx.retried_from_dispatch_run_id = None
        ctx.forked_from_dispatch_run_id = None
        return dispatch_run

    def _record_terminal_dispatch_run(
        self,
        *,
        exit_event: WorkerExit,
        ctx: TaskContext,
        state: str,
    ) -> None:
        prior = self._current_dispatch_run(ctx)
        if prior is None:
            return
        terminal = self._complete_terminal_dispatch_run(
            prior=prior,
            exit_event=exit_event,
            ctx=ctx,
            state=state,
            terminated_at=datetime.now(UTC),
        )
        save_dispatch_run(self.session_root, terminal)

    def _current_dispatch_run(self, ctx: TaskContext) -> DispatchRun | None:
        if ctx.current_dispatch_run_id is None:
            return None
        current_dispatch_run = get_dispatch_run(self.session_root, ctx.current_dispatch_run_id)
        if current_dispatch_run is None:
            return None
        return _dispatch_run_from_row_dict(current_dispatch_run)

    def _complete_terminal_dispatch_run(
        self,
        *,
        prior: DispatchRun,
        exit_event: WorkerExit,
        ctx: TaskContext,
        state: str,
        terminated_at: datetime,
    ) -> DispatchRun:
        common: dict[str, Any] = {
            "output_dir": exit_event.output_dir,
            "prompt_tokens": ctx.prompt_tokens,
            "completion_tokens": ctx.completion_tokens,
            "iteration_count": ctx.call_count,
            "terminated_at": terminated_at,
        }
        if state == "done":
            return prior.complete_done(
                exit_code=exit_event.exit_code,
                **common,
            )
        if state == "timed_out":
            return prior.complete_timed_out(
                last_error=exit_event.last_error or self._ITERATION_EXHAUSTED_MARKER,
                **common,
            )
        if state == "abandoned":
            return prior.complete_abandoned(
                last_error=exit_event.last_error or "Dispatch abandoned",
                **common,
            )
        return prior.complete_failed(
            exit_code=exit_event.exit_code,
            last_error=exit_event.last_error or f"Worker exited with code {exit_event.exit_code}",
            **common,
        )

    def _dispatch_terminal_state(
        self,
        exit_event: WorkerExit,
        *,
        will_fork: bool,
    ) -> DispatchRunState:
        if exit_event.exit_code == 0:
            return "done"
        error = exit_event.last_error or ""
        error_lower = error.lower()
        if (
            will_fork
            or self._ITERATION_EXHAUSTED_MARKER in error
            or "timed out after" in error_lower
            or "wall-clock timeout" in error_lower
        ):
            return "timed_out"
        return "failed"

    async def _dispatch(self, action: DispatchTask) -> list[DagAction]:
        """Dispatch task to Atomic Headless Worker (Pillar #2)."""
        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)
        ctx = self._ctx(action.task_slug)
        prompt = self._prompts.worker_prompt(action.task_slug, task, ctx.error, ctx.attempts)
        wt = await self._create_dispatch_worktree(action.task_slug)
        ctx.worktree = wt
        agent = self._resolved_task_agent(task)

        try:
            if task.role not in ("researcher", "reviewer"):
                await self._prepare_dispatch_worktree(wt)
            pane_slug = self._dispatch_pane_slug(action.task_slug)
            ctx.pane_slug = pane_slug
            self._pending_dispatches.add(action.task_slug)
            ctx.start_time = time.time()
            task_scope = self._task_scope_payload(action.task_slug, task, pane_slug)
            self._mint_dispatch_run(
                task_slug=action.task_slug,
                wt=wt,
                agent=agent,
                ctx=ctx,
            )
            self._record_dispatch_artifact(action, task, wt, prompt, agent)
            kernel_actions = self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))
            self._emit_task_dispatched(action, pane_slug, agent)
            dispatch_task = self._dispatch_task_spec(task, prompt, agent, has_error=ctx.error)
            self._reset_dispatch_fork_state(ctx)
            ctx.worker_task = self._launch_dispatch_worker(
                action=action,
                pane_slug=pane_slug,
                wt=wt,
                dispatch_task=dispatch_task,
                task_scope=task_scope,
                output_dir=output_dir,
                timeout_s=task.timeout_s,
            )
            return kernel_actions

        except Exception as exc:
            await self._handle_dispatch_launch_failure(action.task_slug, wt, ctx, exc)
            raise

    async def _create_dispatch_worktree(self, task_slug: str) -> Worktree:
        base_ref = self._base_ref_for_task(task_slug)
        return await asyncio.to_thread(create_worktree, self.session_root, task_slug, base_ref)

    async def _prepare_dispatch_worktree(self, wt: Worktree) -> None:
        await asyncio.to_thread(
            prepare_worktree,
            wt,
            language=self.project_config.language,
            setup_cmd=self.project_config.setup_cmd or "",
            timeout_s=self.project_config.bootstrap_timeout,
        )

    def _resolved_task_agent(self, task: DagTaskSpec) -> str:
        agent = task.agent
        if agent in self.project_config.agents:
            agent = self.project_config.agents[agent]
        return agent

    def _dispatch_pane_slug(self, task_slug: str) -> str:
        return f"headless-{task_slug}-{uuid.uuid4().hex[:8]}"

    def _task_scope_payload(
        self,
        task_slug: str,
        task: DagTaskSpec,
        pane_slug: str,
    ) -> dict[str, object]:
        return {
            "task_slug": task_slug,
            "session_root": self.session_root,
            "pane_slug": pane_slug,
            "create": list(task.files.create),
            "edit": list(task.files.edit),
            "delete": list(task.files.delete),
            "touch": list(task.files.touch),
            "read": list(task.files.read),
            "scope_ignore_files": list(self.project_config.scope_ignore_files),
            "verify_test_targets": list(_verify_test_targets(task, self.project_config.test_dir)),
        }

    def _record_dispatch_artifact(
        self,
        action: DispatchTask,
        task: DagTaskSpec,
        wt: Worktree,
        prompt: str,
        agent: str,
    ) -> None:
        record_runtime_artifact(
            self.session_root,
            WorkerTask(
                slug=action.task_slug,
                prompt=prompt,
                agent=agent,
                project_root=self.session_root,
                worktree_path=str(wt.path),
                branch_name=wt.branch,
                role=task.role,
                state=TaskState.ACTIVE,
                plan_name=self.dag.name,
                file_claims=self._file_claims_for_task(task),
            ),
        )

    def _file_claims_for_task(self, task: DagTaskSpec) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                task.files.create + task.files.edit + task.files.delete + task.files.touch
            )
        )

    def _emit_task_dispatched(self, action: DispatchTask, pane_slug: str, agent: str) -> None:
        emit_event(
            self.session_root,
            EvtTaskDispatched(
                pane=pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                agent=agent,
            ),
        )

    def _dispatch_task_spec(
        self,
        task: DagTaskSpec,
        prompt: str,
        agent: str,
        *,
        has_error: str | None,
    ) -> DagTaskSpec:
        dispatch_task = task if not has_error else task.model_copy(update={"prompt": prompt})
        return dispatch_task.model_copy(update={"agent": agent})

    def _reset_dispatch_fork_state(self, ctx: TaskContext) -> None:
        ctx.fork_depth = 0
        ctx.call_count = 0

    def _launch_dispatch_worker(
        self,
        *,
        action: DispatchTask,
        pane_slug: str,
        wt: Worktree,
        dispatch_task: DagTaskSpec,
        task_scope: dict[str, object],
        output_dir: Path,
        timeout_s: int,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._run_with_timeout(
                action.task_slug,
                pane_slug,
                wt.path,
                dispatch_task,
                task_scope,
                self._worker_exit_callback(output_dir),
                timeout_s,
                on_event=self._make_counted_on_event(action.task_slug),
            )
        )

    def _worker_exit_callback(
        self,
        output_dir: Path,
    ) -> Callable[[str, str, int, str, int, int], None]:
        def _on_worker_exit(
            task_slug: str,
            pane_slug: str,
            exit_code: int,
            last_error: str = "",
            prompt_tokens: int = 0,
            completion_tokens: int = 0,
        ) -> None:
            self._push_worker_exit(
                task_slug=task_slug,
                pane_slug=pane_slug,
                exit_code=exit_code,
                output_dir=str(output_dir),
                last_error=last_error,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return _on_worker_exit

    def _push_worker_exit(
        self,
        *,
        task_slug: str,
        pane_slug: str,
        exit_code: int,
        output_dir: str,
        last_error: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self._event_queue.put_nowait(
            WorkerExit(
                task_slug=task_slug,
                pane_slug=pane_slug,
                exit_code=exit_code,
                output_dir=output_dir,
                last_error=last_error,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    async def _handle_dispatch_launch_failure(
        self,
        task_slug: str,
        wt: Worktree,
        ctx: TaskContext,
        exc: Exception,
    ) -> None:
        logger.error("Dispatch failed for %s after worktree creation: %s", task_slug, exc)
        ctx.error = str(exc)
        self._pending_dispatches.discard(task_slug)
        self._record_terminal_dispatch_run(
            exit_event=WorkerExit(
                task_slug=task_slug,
                pane_slug=ctx.pane_slug or "dispatch",
                exit_code=1,
                output_dir="",
                last_error=str(exc),
            ),
            ctx=ctx,
            state="failed",
        )
        try:
            await asyncio.to_thread(remove_worktree, self.session_root, wt)
        except Exception as cleanup_exc:
            logger.warning("Worktree cleanup after dispatch failure: %s", cleanup_exc)
        ctx.worktree = None

    def _emit_settlement_phase_started(self, action: MergeTask, phase: str) -> None:
        """Emit settlement_phase_started event."""
        emit_event(
            self.session_root,
            SettlementPhaseStarted(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                phase=phase,
            ),
        )

    def _emit_settlement_phase_completed(
        self,
        action: MergeTask,
        phase: str,
        status: str,
        duration_s: float,
        error: str | None = None,
    ) -> None:
        """Emit settlement_phase_completed event."""
        emit_event(
            self.session_root,
            SettlementPhaseCompleted(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                phase=phase,
                status=status,
                duration_s=duration_s,
                error=error,
            ),
        )

    async def _settle_and_merge(
        self,
        action: MergeTask,
        wt: Worktree,
    ) -> tuple[str | None, bool]:
        """Run settlement phases: prepare → validate → candidate → merge.

        Phase-split for testability: each phase can fail independently.
        Returns (error, was_settlement) tuple.
        """
        task = self.dag.tasks[action.task_slug]

        error, was_settlement = await self._prepare_commit_phase(action, wt, task)
        if error or was_settlement is False:
            return error, was_settlement

        error, risk_record = await self._isolated_validation_phase(action, wt, task)
        if error or risk_record is None:
            return error, True

        error, candidate_result = await self._integration_candidate_phase(action, wt)
        if error or candidate_result is None:
            return error, True

        error = await self._semantic_gate_phase(action, wt, candidate_result, risk_record)
        if error:
            return error, True

        error = await self._candidate_validation_phase(action, task, candidate_result)
        if error:
            return error, True

        await self._final_merge_phase(action, wt)
        return None, False

    async def _prepare_commit_phase(
        self,
        action: MergeTask,
        wt: Worktree,
        task: DagTaskSpec,
    ) -> tuple[str | None, bool]:
        phase = "prepare_commit"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error, was_settlement = await self._settlement_flow.prepare_and_commit(
            task=task,
            action=action,
            wt=wt,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if error:
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, was_settlement
        if was_settlement is False:
            self._emit_settlement_phase_completed(action, phase, "skipped", duration)
            return error, was_settlement
        self._emit_settlement_phase_completed(action, phase, "passed", duration)
        return None, was_settlement

    async def _isolated_validation_phase(
        self,
        action: MergeTask,
        wt: Worktree,
        task: DagTaskSpec,
    ) -> tuple[str | None, IntegrationRiskRecord | None]:
        phase = "isolated_validation"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error, risk_record = await self._settlement_flow.run_isolated_validation(
            task=task,
            action=action,
            wt=wt,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if error or risk_record is None:
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, risk_record
        if risk_record.risk_level == RiskLevel.CRITICAL:
            crit_error = f"Integration risk CRITICAL: {_summarize_evidence(risk_record)}"
            self._emit_settlement_phase_completed(action, phase, "failed", duration, crit_error)
            return crit_error, risk_record
        self._emit_settlement_phase_completed(action, phase, "passed", duration)
        return None, risk_record

    async def _integration_candidate_phase(
        self,
        action: MergeTask,
        wt: Worktree,
    ) -> tuple[str | None, Any | None]:
        phase = "integration_candidate"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        candidate_result = await self._settlement_flow.create_integration_candidate_with_emit(
            action=action,
            wt=wt,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if not candidate_result.passed:
            error = self._settlement_flow.integration_candidate_failure_message(candidate_result)
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, candidate_result
        self._emit_settlement_phase_completed(action, phase, "passed", duration)
        return None, candidate_result

    async def _semantic_gate_phase(
        self,
        action: MergeTask,
        wt: Worktree,
        candidate_result: Any,
        risk_record: IntegrationRiskRecord,
    ) -> str | None:
        phase = "semantic_gate"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error = await self._settlement_flow.run_semantic_gate_on_candidate(
            action=action,
            wt=wt,
            candidate_result=candidate_result,
            risk_record=risk_record,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        status = "failed" if error else "passed"
        self._emit_settlement_phase_completed(action, phase, status, duration, error)
        return error

    async def _candidate_validation_phase(
        self,
        action: MergeTask,
        task: DagTaskSpec,
        candidate_result: Any,
    ) -> str | None:
        phase = "candidate_validation"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error = await self._settlement_flow.validate_and_finalize_candidate(
            action=action,
            candidate_result=candidate_result,
            project_config=self.project_config,
            task_test_cmd=task.test_cmd,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        status = "failed" if error else "passed"
        self._emit_settlement_phase_completed(action, phase, status, duration, error)
        return error

    async def _final_merge_phase(self, action: MergeTask, wt: Worktree) -> None:
        phase = "final_merge"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        try:
            await self._settlement_flow.finalize_merge(action=action, wt=wt)
            duration = time.monotonic() - start_ts
            self._emit_settlement_phase_completed(action, phase, "passed", duration)
        except Exception as exc:
            duration = time.monotonic() - start_ts
            self._emit_settlement_phase_completed(action, phase, "failed", duration, str(exc))
            raise

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
        retry_task = DagTaskSpec(
            slug=action.task_slug,
            summary=f"[retry] {task.summary}",
            prompt=PromptBuilder.settlement_retry_prompt(task, settlement_error),
            commit_message=task.commit_message,
            depends_on=task.depends_on,
            files=task.files,
            agent=task.agent,
            timeout_s=task.timeout_s,
            test_cmd=task.test_cmd,
        )
        retry_pane_slug = f"{action.pane_slug}-retry"
        retry_scope = self._retry_scope(action.task_slug, task, retry_pane_slug)
        if (command := _test_failure_command(settlement_error)) and retry_scope[
            "verify_test_targets"
        ]:
            retry_scope["require_successful_test_verification"] = True
            retry_scope["required_verification_command"] = command

        await run_headless_worker(
            self.session_root,
            self.dag.name,
            action.task_slug,
            retry_pane_slug,
            wt.path,
            retry_task,
            retry_scope,
            self._noop_retry_exit,
            on_event=self.on_event,
        )

    def _retry_scope(
        self,
        task_slug: str,
        task: DagTaskSpec,
        pane_slug: str = "",
    ) -> dict[str, object]:
        return {
            "task_slug": task_slug,
            "session_root": self.session_root,
            "pane_slug": pane_slug,
            "create": list(task.files.create),
            "edit": list(task.files.edit),
            "delete": list(task.files.delete),
            "touch": list(task.files.touch),
            "read": list(task.files.read),
            "scope_ignore_files": list(self.project_config.scope_ignore_files),
            "verify_test_targets": list(_verify_test_targets(task, self.project_config.test_dir)),
        }

    def _noop_retry_exit(
        self,
        slug: str,
        pane: str,
        code: int,
        err: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        if code != 0:
            logger.warning("Settlement retry worker exited %d for %s: %s", code, slug, err)

    async def _retry_after_settlement_rejection(
        self,
        action: MergeTask,
        wt: Worktree,
        error: str,
    ) -> tuple[str | None, bool]:
        logger.info("SETTLEMENT RETRY %s — feeding error back to worker", action.task_slug)
        emit_event(
            self.session_root,
            SettlementRetry(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                error=error,
            ),
        )
        await self._settlement_retry(action, wt, error)
        async with self._settlement_semaphore:
            retry_error, settlement_rejected = await self._settle_and_merge(action, wt)
        if settlement_rejected:
            logger.warning("REJECTED %s after retry: %s", action.task_slug, retry_error)
        return retry_error, settlement_rejected

    async def _cleanup_merged_worktree(
        self,
        *,
        action: MergeTask,
        wt: Worktree,
        settlement_rejected: bool,
    ) -> None:
        ctx = self._ctx(action.task_slug)
        if settlement_rejected:
            logger.info("Worktree preserved for inspection: %s (%s)", action.task_slug, wt.path)
            ctx.worktree = None
            ctx.rejected_worktree = wt
            return

        try:
            await asyncio.to_thread(remove_worktree, self.session_root, wt)
        except Exception as exc:
            logger.warning("Worktree cleanup failed for %s: %s", action.task_slug, exc)
        ctx.worktree = None

    def _sync_merge_artifact_state(self, task_slug: str, error: str | None) -> None:
        db_state = TaskState.MERGED if not error else TaskState.FAILED
        try:
            update_runtime_artifact_state(
                self.session_root,
                task_slug,
                db_state.value,
                force=True,
            )
        except Exception as exc:
            logger.warning("DB state sync failed for %s: %s", task_slug, exc)

    def _emit_merge_completion(self, action: MergeTask, error: str | None) -> None:
        if error:
            emit_event(
                self.session_root,
                TaskMergeFailed(
                    pane=action.pane_slug,
                    plan_name=self.dag.name,
                    task_slug=action.task_slug,
                    error=error,
                ),
            )
        else:
            emit_event(
                self.session_root,
                MergeCompleted(
                    pane=action.pane_slug,
                    plan_name=self.dag.name,
                    task_slug=action.task_slug,
                    error=None,
                ),
            )

    async def _merge(self, action: MergeTask) -> list[DagAction]:
        """Commit-or-Kill: Merge worktree branch into base (Pillar #2)."""
        action = self._merge_action_with_context(action)
        ctx = self._tasks.get(action.task_slug)
        wt = ctx.worktree if ctx else None
        if not wt:
            return self.kernel.handle(TaskMergeDone(action.task_slug))

        error = None
        settlement_rejected = False

        try:
            async with self._settlement_semaphore:
                error, settlement_rejected = await self._settle_and_merge(action, wt)

            if settlement_rejected and error:
                error, settlement_rejected = await self._retry_after_settlement_rejection(
                    action, wt, error
                )
        except Exception as exc:
            logger.error("Merge execution failed for %s: %s", action.task_slug, exc)
            error = str(exc)

        await self._cleanup_merged_worktree(
            action=action,
            wt=wt,
            settlement_rejected=settlement_rejected,
        )

        if error:
            self._ctx(action.task_slug).error = error

        actions = self.kernel.handle(TaskMergeDone(action.task_slug, error=error))

        self._sync_merge_artifact_state(action.task_slug, error)
        self._emit_merge_completion(action, error)
        return actions
