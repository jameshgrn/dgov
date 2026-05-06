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
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
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
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.prompt_builder import PromptBuilder, build_baseline_diag_note, load_review_sop_blocks
from dgov.settlement import review_sandbox
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
    evidence = risk_record.overlap_evidence
    if not evidence:
        return "no semantic evidence"

    parts: list[str] = []
    for item in evidence:
        kind = item.__class__.__name__
        symbol = getattr(item, "symbol_name", "")
        file_path = getattr(item, "file_path", "")
        file_paths = getattr(item, "file_paths", ())
        location = file_path or ", ".join(str(path) for path in file_paths)
        detail = ": ".join(part for part in (symbol, location) if part)
        parts.append(f"{kind}({detail})" if detail else kind)
    return "; ".join(parts)


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
        self.task_read_files = {slug: tuple(t.files.read) for slug, t in dag.tasks.items()}
        self._tasks: dict[str, TaskContext] = {}
        self.kernel = DagKernel(
            deps=self.deps,
            task_files=self.task_files,
            max_retries=dag.default_max_retries,
        )
        self._prompts = PromptBuilder(
            session_root=session_root,
            dag=dag,
            baseline_diag_note=build_baseline_diag_note(self.project_config, session_root),
            review_sop_blocks=load_review_sop_blocks(session_root),
        )
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

        if restart:
            reset_plan_state(session_root, dag.name)
        else:
            self._run_recovery_pipeline(continue_failed=continue_failed)

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

    def _apply_rehydrate_event(self, event: DgovEvent) -> None:
        """Apply a single event during rehydration. Extracted for testability."""
        task_slug = getattr(event, "task_slug", None)
        pane = getattr(event, "pane", "")

        if not task_slug or task_slug not in self.kernel.task_states:
            return
        if pane:
            self._ctx(task_slug).pane_slug = pane

        if isinstance(event, EvtTaskDispatched):
            self.kernel.handle(TaskDispatched(task_slug, pane))
        elif isinstance(event, TaskDone):
            self.kernel.handle(TaskWaitDone(task_slug, pane, TaskState.DONE))
        elif isinstance(event, TaskAbandoned):
            self.kernel.handle(TaskWaitDone(task_slug, pane, TaskState.ABANDONED))
        elif isinstance(event, TaskFailed):
            # Check error string for specific terminal states like TIMED_OUT
            error = (event.error or "").lower()
            status = TaskState.FAILED
            if "timeout" in error:
                status = TaskState.TIMED_OUT
            self.kernel.handle(TaskWaitDone(task_slug, pane, status))
        elif isinstance(event, ReviewPass):
            self.kernel.handle(
                TaskReviewDone(task_slug, passed=True, verdict="rehydrated", commit_count=1)
            )
        elif isinstance(event, ReviewFail):
            self.kernel.handle(
                TaskReviewDone(task_slug, passed=False, verdict="rehydrated", commit_count=0)
            )
        elif isinstance(event, MergeCompleted):
            self.kernel.handle(TaskMergeDone(task_slug, error=None))
        elif isinstance(event, TaskMergeFailed):
            self.kernel.handle(TaskMergeDone(task_slug, error=event.error or "unknown error"))
        elif isinstance(event, GovernorResumed):
            # Restore attempt counts and retry/skip/fail state
            action_str = event.action or None
            if action_str:
                try:
                    action = GovernorAction(action_str)
                    self.kernel.handle(TaskGovernorResumed(task_slug, action))
                    # Restore attempt count in runner (kernel no longer tracks this)
                    if action == GovernorAction.RETRY:
                        self._ctx(task_slug).attempts += 1
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
            ShutdownRequested(
                pane="runner",
                plan_name=self.dag.name,
                reason="signal",
            ),
        )

    async def _cleanup(self) -> None:
        """Cleanup all resources — worktrees, executor, connections (Pillar #3, #10)."""
        active_workers = [
            (slug, ctx.worker_task) for slug, ctx in self._tasks.items() if ctx.worker_task
        ]
        active_wts = [ctx.worktree for ctx in self._tasks.values() if ctx.worktree]
        rejected_wts = [
            ctx.rejected_worktree for ctx in self._tasks.values() if ctx.rejected_worktree
        ]
        logger.info(
            "Cleaning up %d worker tasks, %d worktrees",
            len(active_workers),
            len(active_wts) + len(rejected_wts),
        )

        # Cancel active worker asyncio tasks first — stop work before removing worktrees
        for task_slug, atask in active_workers:
            if not atask.done():
                atask.cancel()
                logger.debug("Cancelled worker task: %s", task_slug)

        # Wait briefly for cancellations to propagate
        if active_workers:
            await asyncio.gather(*[atask for _, atask in active_workers], return_exceptions=True)
        for ctx in self._tasks.values():
            ctx.worker_task = None
        self._pending_dispatches.clear()

        # Close all worktrees (including rejected ones for total cleanup ONLY if shutdown was set)
        to_clean = list(active_wts)
        if self._shutdown_event.is_set():
            to_clean += rejected_wts

        for wt in to_clean:
            try:
                await asyncio.to_thread(remove_worktree, self.session_root, wt)
            except Exception as exc:
                logger.warning("Failed to remove worktree: %s", exc)

        for ctx in self._tasks.values():
            ctx.worktree = None
            if self._shutdown_event.is_set():
                ctx.rejected_worktree = None

        logger.info("Cleanup complete")

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
                structural = self._run_structural_review(action)
                if structural is not None:
                    next_actions.extend(structural)
                else:
                    dispatch_coros.append((action.task_slug, self._run_self_review_gate(action)))
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
        ctx.worker_task = None
        ctx.prompt_tokens += exit_event.prompt_tokens
        ctx.completion_tokens += exit_event.completion_tokens

        if exit_event.last_error:
            ctx.error = exit_event.last_error

        # --- Clean-context fork on iteration exhaustion ---
        task = self.dag.tasks[exit_event.task_slug]
        if (
            exit_event.exit_code != 0
            and self._ITERATION_EXHAUSTED_MARKER in (exit_event.last_error or "")
            and ctx.fork_depth < task.max_fork_depth
            and ctx.worktree is not None
        ):
            ctx.fork_depth += 1
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
            ctx.worker_task = asyncio.create_task(
                self._fork_worker(exit_event.task_slug, ctx.worktree, exit_event.pane_slug)
            )
            return []  # kernel still sees task as ACTIVE

        status = TaskState.DONE if exit_event.exit_code == 0 else TaskState.FAILED

        # Calculate task duration
        start_time = ctx.start_time
        ctx.start_time = None
        duration = round(time.time() - start_time, 2) if start_time else None
        if duration is not None:
            ctx.duration = duration

        actions = self.kernel.handle(
            TaskWaitDone(exit_event.task_slug, exit_event.pane_slug, status)
        )
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
        else:
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
        return actions

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

        def _push_exit(
            slug: str,
            pane: str,
            code: int,
            err: str = "",
            prompt_tokens: int = 0,
            completion_tokens: int = 0,
        ) -> None:
            exit_event = WorkerExit(
                task_slug=slug,
                pane_slug=pane,
                exit_code=code,
                output_dir="",
                last_error=err,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            self._event_queue.put_nowait(exit_event)

        try:
            diff_text = await self._get_worktree_diff(wt) or "(no diff available)"
            handoff_prompt = PromptBuilder.fork_handoff_prompt(task, diff_text)

            forked_task = task.model_copy(
                update={
                    "summary": f"[fork-{ctx.fork_depth}] {task.summary}",
                    "prompt": handoff_prompt,
                }
            )
            task_scope = self._retry_scope(task_slug, task)
            counted_on_event = self._make_counted_on_event(task_slug)

            await asyncio.wait_for(
                run_headless_worker(
                    self.session_root,
                    self.dag.name,
                    task_slug,
                    fork_pane,
                    wt.path,
                    forked_task,
                    task_scope,
                    _push_exit,
                    on_event=counted_on_event,
                ),
                timeout=float(task.timeout_s) if task.timeout_s > 0 else None,
            )
        except TimeoutError:
            logger.error("Forked worker %s timed out after %ds", task_slug, task.timeout_s)
            _push_exit(task_slug, fork_pane, 1, f"Fork timed out after {task.timeout_s}s")
        except Exception as exc:
            logger.error("Fork failed for %s: %s", task_slug, exc)
            _push_exit(task_slug, fork_pane, 1, f"Fork failed: {exc}")

    # ---- Self-review (clean-context semantic review) ----

    async def _run_self_review_gate(self, action: ReviewTask) -> list[DagAction]:
        """Async self-review gate — runs after structural review passes."""
        task = self.dag.tasks[action.task_slug]
        ctx = self._ctx(action.task_slug)
        wt = ctx.worktree
        if not wt:
            return self.kernel.handle(
                TaskReviewDone(
                    action.task_slug,
                    passed=True,
                    verdict="self_review_skipped",
                    commit_count=ctx.review_file_count,
                )
            )

        emit_event(
            self.session_root,
            ReviewPass(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict="structural_pass",
            ),
        )

        # Self-review is advisory — any failure auto-passes to settlement
        try:
            passed, findings = await self._run_self_review(action.task_slug, wt, action.pane_slug)

            if passed:
                emit_event(
                    self.session_root,
                    SelfReviewPassed(
                        pane=action.pane_slug,
                        plan_name=self.dag.name,
                        task_slug=action.task_slug,
                    ),
                )
            else:
                emit_event(
                    self.session_root,
                    SelfReviewRejected(
                        pane=action.pane_slug,
                        plan_name=self.dag.name,
                        task_slug=action.task_slug,
                        findings=findings or "",
                    ),
                )
                # Re-launch worker in same worktree with findings
                await self._relaunch_worker_with_findings(
                    action.task_slug, wt, findings or "", task
                )
                # Second self-review — auto-pass regardless of outcome
                passed2, findings2 = await self._run_self_review(
                    action.task_slug, wt, action.pane_slug
                )
                if passed2:
                    emit_event(
                        self.session_root,
                        SelfReviewPassed(
                            pane=action.pane_slug,
                            plan_name=self.dag.name,
                            task_slug=action.task_slug,
                        ),
                    )
                else:
                    emit_event(
                        self.session_root,
                        SelfReviewAutoPassed(
                            pane=action.pane_slug,
                            plan_name=self.dag.name,
                            task_slug=action.task_slug,
                            findings=findings2,
                        ),
                    )
        except Exception as exc:
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

        # Always pass to settlement
        return self.kernel.handle(
            TaskReviewDone(
                action.task_slug,
                passed=True,
                verdict="self_review_complete",
                commit_count=ctx.review_file_count,
            )
        )

    async def _run_self_review(
        self,
        task_slug: str,
        wt: Worktree,
        pane_slug: str,
    ) -> tuple[bool, str | None]:
        """Spawn a clean-context reviewer on the diff. Returns (passed, findings)."""
        import json

        task = self.dag.tasks[task_slug]

        diff_text = await self._get_worktree_diff(wt)
        if not diff_text.strip():
            return True, None

        review_prompt = self._prompts.self_review_prompt(diff_text)

        # Capture reviewer output via on_event
        captured: list[str] = []

        def _capture(slug: str, log_type: str, content: object) -> None:
            if log_type == "done" and content:
                captured.append(str(content))
            if self.on_event is not None:
                self.on_event(f"{task_slug}/self-review", log_type, content)

        reviewer_task = DagTaskSpec(
            slug=f"{task_slug}-self-review",
            summary=f"Semantic review of {task_slug}",
            prompt=review_prompt,
            role="reviewer",
            agent=task.agent,
            timeout_s=120,
        )
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
                on_event=_capture,
            ),
            timeout=120.0,
        )

        if not captured:
            return True, None

        output = captured[-1]
        try:
            verdict = json.loads(output)
            if verdict.get("approved", True):
                return True, None
            issues = verdict.get("issues", [])
            return False, "\n".join(f"- {issue}" for issue in issues)
        except (json.JSONDecodeError, AttributeError):
            # Fallback: keyword scan for natural-language responses
            lower = output.lower()
            if any(w in lower for w in ("no issues", "looks good", "approved", "lgtm")):
                return True, None
            if any(w in lower for w in ("issue", "bug", "error", "incorrect", "wrong", "missing")):
                return False, output
            return True, None

    async def _relaunch_worker_with_findings(
        self,
        task_slug: str,
        wt: Worktree,
        findings: str,
        task: DagTaskSpec,
    ) -> None:
        """Re-launch worker in same worktree with self-review findings."""
        fix_prompt = (
            "A semantic review of your changes found the following issues:\n\n"
            f"{findings}\n\n"
            "Fix these issues in the current worktree, then call done.\n"
            "Use git_diff to see your current changes.\n\n"
            f"ORIGINAL TASK:\n{task.prompt or ''}"
        )
        fix_task = task.model_copy(
            update={
                "summary": f"[review-fix] {task.summary}",
                "prompt": fix_prompt,
            }
        )
        fix_scope = self._retry_scope(task_slug, task)

        emit_event(
            self.session_root,
            SelfReviewFixStarted(
                pane=self._ctx(task_slug).pane_slug or "",
                plan_name=self.dag.name,
                task_slug=task_slug,
            ),
        )

        await asyncio.wait_for(
            run_headless_worker(
                self.session_root,
                self.dag.name,
                task_slug,
                f"{self._ctx(task_slug).pane_slug or ''}-review-fix",
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
            if actions:
                next_actions, final = await self._process_actions(actions)
                if final is not None:
                    return final
                if next_actions:
                    actions = next_actions
                    continue
                actions = []

            if self._shutdown_event.is_set():
                actions = self._abandon_active_tasks_for_shutdown()
                if actions:
                    continue
                self._shutdown_interrupted = True
                break

            if self.kernel.done:
                break

            # Wait for either a worker exit or shutdown request.
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
                    exit_event = queue_get.result()
                    actions = self._handle_worker_exit(exit_event)
                    continue
                if shutdown_wait in done:
                    continue
                if not self._pending_dispatches and self.kernel.done:
                    break
            except TimeoutError:
                if not self._pending_dispatches and self.kernel.done:
                    break
            finally:
                if not queue_get.done():
                    queue_get.cancel()
                if not shutdown_wait.done():
                    shutdown_wait.cancel()
                await asyncio.gather(queue_get, shutdown_wait, return_exceptions=True)

        return self._task_state_snapshot()

    def _run_structural_review(self, action: ReviewTask) -> list[DagAction] | None:
        """Structural review gate (scope check). Returns None if self-review needed."""
        task = self.dag.tasks[action.task_slug]

        # Read-only roles (researcher, reviewer) produce no code changes.
        # Auto-pass review — their output is in the event log, not the worktree.
        if task.role in ("researcher", "reviewer"):
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

        ctx = self._tasks.get(action.task_slug)
        wt = ctx.worktree if ctx else None
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
        read_files = self.task_read_files.get(action.task_slug, ())
        review_result = review_sandbox(
            wt.path,
            claimed_files=claimed_files,
            read_files=read_files,
            project_root=self.session_root,
            task_slug=action.task_slug,
            pane_slug=action.pane_slug,
            scope_ignore_files=self.project_config.scope_ignore_files,
        )

        if not review_result.passed:
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
            if review_result.error:
                error_msg = f"review:{review_result.verdict} — {review_result.error}"
                if review_result.verdict == "scope_violation":
                    error_msg += (
                        f"\nhint: add these paths to files.edit in task"
                        f" '{action.task_slug}', then recompile and re-run"
                    )
                self._ctx(action.task_slug).error = error_msg
            return self.kernel.handle(
                TaskReviewDone(
                    action.task_slug,
                    passed=False,
                    verdict=review_result.verdict,
                    commit_count=len(review_result.actual_files),
                )
            )

        # Structural review passed — defer to async self-review if enabled
        if task.self_review and task.role == "worker":
            ctx = self._ctx(action.task_slug)
            ctx.review_file_count = len(review_result.actual_files)
            return None  # signal: route to _run_self_review_gate

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

    def _handle_interrupt(self, action: InterruptGovernor) -> list[DagAction]:
        """Decide retry vs fail based on attempt count."""
        ctx = self._ctx(action.task_slug)
        attempts = ctx.attempts
        error_detail = ctx.error or ""

        if self._shutdown_event.is_set():
            self._shutdown_interrupted = True
            logger.warning(
                "Task %s interrupted during shutdown — marking ABANDONED: %s",
                action.task_slug,
                error_detail or action.reason,
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

        gov_action = GovernorAction.FAIL
        if error_detail in self._NON_RETRYABLE_ERRORS:
            logger.error(
                "Task %s failed — non-retryable: %s",
                action.task_slug,
                error_detail,
            )
        elif attempts < self.kernel.max_retries:
            ctx.attempts = attempts + 1
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
            GovernorResumed(
                pane=action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                action=gov_action.value,
            ),
        )
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

    async def _dispatch(self, action: DispatchTask) -> list[DagAction]:
        """Dispatch task to Atomic Headless Worker (Pillar #2)."""
        import uuid

        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build worker prompt with all enrichments (baseline diag, ledger probation, retry context)
        ctx = self._ctx(action.task_slug)
        prompt = self._prompts.worker_prompt(action.task_slug, task, ctx.error, ctx.attempts)

        # Pillar #3: Snapshot Isolation
        base_ref = self._base_ref_for_task(action.task_slug)
        wt = await asyncio.to_thread(
            create_worktree, self.session_root, action.task_slug, base_ref
        )
        ctx.worktree = wt

        # Re-resolve agent from project config mapping
        agent = task.agent
        if agent in self.project_config.agents:
            agent = self.project_config.agents[agent]

        try:
            if task.role not in ("researcher", "reviewer"):
                await asyncio.to_thread(
                    prepare_worktree,
                    wt,
                    language=self.project_config.language,
                    setup_cmd=self.project_config.setup_cmd or "",
                    timeout_s=self.project_config.bootstrap_timeout,
                )
            pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
            ctx.pane_slug = pane_slug
            self._pending_dispatches.add(action.task_slug)
            ctx.start_time = time.time()
            task_scope = {
                "task_slug": action.task_slug,
                "create": list(task.files.create),
                "edit": list(task.files.edit),
                "delete": list(task.files.delete),
                "touch": list(task.files.touch),
                "read": list(task.files.read),
                "verify_test_targets": list(
                    _verify_test_targets(task, self.project_config.test_dir)
                ),
            }

            # Record runtime artifact metadata for cleanup and debugging only.
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
            record_runtime_artifact(self.session_root, task_record)

            # Atomic transition to WAITING
            kernel_actions = self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

            emit_event(
                self.session_root,
                EvtTaskDispatched(
                    pane=pane_slug,
                    plan_name=self.dag.name,
                    task_slug=action.task_slug,
                    agent=agent,
                ),
            )

            def _on_worker_exit(
                task_slug: str,
                pane_slug: str,
                exit_code: int,
                last_error: str = "",
                prompt_tokens: int = 0,
                completion_tokens: int = 0,
            ) -> None:
                exit_event = WorkerExit(
                    task_slug=task_slug,
                    pane_slug=pane_slug,
                    exit_code=exit_code,
                    output_dir=str(output_dir),
                    last_error=last_error,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                self._event_queue.put_nowait(exit_event)

            dispatch_task = task if not ctx.error else task.model_copy(update={"prompt": prompt})
            # Use re-resolved agent
            dispatch_task = dispatch_task.model_copy(update={"agent": agent})
            timeout_s = task.timeout_s
            # Reset fork state on fresh dispatch (governor retry starts clean)
            ctx.fork_depth = 0
            ctx.call_count = 0
            counted_on_event = self._make_counted_on_event(action.task_slug)
            worker_task = asyncio.create_task(
                self._run_with_timeout(
                    action.task_slug,
                    pane_slug,
                    wt.path,
                    dispatch_task,
                    task_scope,
                    _on_worker_exit,
                    timeout_s,
                    on_event=counted_on_event,
                )
            )
            ctx.worker_task = worker_task
            return kernel_actions

        except Exception as exc:
            # Worktree created but launch failed — clean up to prevent leak
            logger.error(
                "Dispatch failed for %s after worktree creation: %s", action.task_slug, exc
            )
            ctx.error = str(exc)
            self._pending_dispatches.discard(action.task_slug)
            try:
                await asyncio.to_thread(remove_worktree, self.session_root, wt)
            except Exception as cleanup_exc:
                logger.warning("Worktree cleanup after dispatch failure: %s", cleanup_exc)
            ctx.worktree = None
            raise

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
        pc = self.project_config
        task_config = replace(pc, test_cmd=task.test_cmd) if task.test_cmd else pc
        sf = self._settlement_flow

        # Phase 1: Prepare and commit (or handle read-only roles)
        phase = "prepare_commit"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error, was_settlement = await sf.prepare_and_commit(
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

        # Phase 2: Isolated validation (compute risk + run gate)
        phase = "isolated_validation"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error, risk_record = await sf.run_isolated_validation(
            task=task,
            action=action,
            wt=wt,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if error or risk_record is None:
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, True
        if risk_record.risk_level == RiskLevel.CRITICAL:
            crit_error = f"Integration risk CRITICAL: {_summarize_evidence(risk_record)}"
            self._emit_settlement_phase_completed(action, phase, "failed", duration, crit_error)
            return crit_error, True
        self._emit_settlement_phase_completed(action, phase, "passed", duration)

        # Phase 3: Create integration candidate
        phase = "integration_candidate"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        candidate_result = await sf.create_integration_candidate_with_emit(
            action=action,
            wt=wt,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if not candidate_result.passed:
            cand_error = candidate_result.error or "Integration candidate replay failed"
            self._emit_settlement_phase_completed(action, phase, "failed", duration, cand_error)
            return cand_error, True
        self._emit_settlement_phase_completed(action, phase, "passed", duration)

        # Phase 4: Run semantic gate on candidate
        phase = "semantic_gate"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error = await sf.run_semantic_gate_on_candidate(
            action=action,
            wt=wt,
            candidate_result=candidate_result,
            risk_record=risk_record,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if error:
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, True
        self._emit_settlement_phase_completed(action, phase, "passed", duration)

        # Phase 5: Validate candidate with same gates as isolated validation
        phase = "candidate_validation"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        error = await sf.validate_and_finalize_candidate(
            action=action,
            candidate_result=candidate_result,
            task_config=task_config,
            emit_event_fn=emit_event,
        )
        duration = time.monotonic() - start_ts
        if error:
            self._emit_settlement_phase_completed(action, phase, "failed", duration, error)
            return error, True
        self._emit_settlement_phase_completed(action, phase, "passed", duration)

        # Phase 6: Final merge and deploy
        phase = "final_merge"
        start_ts = time.monotonic()
        self._emit_settlement_phase_started(action, phase)
        try:
            await sf.finalize_merge(action=action, wt=wt)
            duration = time.monotonic() - start_ts
            self._emit_settlement_phase_completed(action, phase, "passed", duration)
        except Exception as exc:
            duration = time.monotonic() - start_ts
            merge_error = str(exc)
            self._emit_settlement_phase_completed(action, phase, "failed", duration, merge_error)
            raise
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
        retry_scope = self._retry_scope(action.task_slug, task)
        if (command := _test_failure_command(settlement_error)) and retry_scope[
            "verify_test_targets"
        ]:
            retry_scope["require_successful_test_verification"] = True
            retry_scope["required_verification_command"] = command

        await run_headless_worker(
            self.session_root,
            self.dag.name,
            action.task_slug,
            f"{action.pane_slug}-retry",
            wt.path,
            retry_task,
            retry_scope,
            self._noop_retry_exit,
            on_event=self.on_event,
        )

    def _retry_scope(self, task_slug: str, task: DagTaskSpec) -> dict[str, object]:
        return {
            "task_slug": task_slug,
            "create": list(task.files.create),
            "edit": list(task.files.edit),
            "delete": list(task.files.delete),
            "touch": list(task.files.touch),
            "read": list(task.files.read),
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
