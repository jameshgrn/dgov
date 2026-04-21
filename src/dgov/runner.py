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
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
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
from dgov.config import ProjectConfig
from dgov.dag_parser import DagDefinition, DagTaskSpec
from dgov.kernel import DagKernel
from dgov.live_state import latest_run_start_ids
from dgov.persistence import (
    emit_event,
    record_runtime_artifact,
    update_runtime_artifact_state,
)
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.settlement import review_sandbox
from dgov.settlement_flow import (
    IntegrationCandidateResult,
    IntegrationCandidateVerdict,
    IntegrationRiskRecord,
    SettlementFlow,
    autofix_sandbox,
    commit_in_worktree,
    create_integration_candidate,
    emit_integration_candidate_failed,
    emit_integration_candidate_passed,
    emit_semantic_gate_rejected,
    merge_worktree,
    remove_integration_candidate,
    run_python_semantic_gate_in_subprocess,
    validate_sandbox,
)
from dgov.types import WorkerExit, Worktree
from dgov.workers.headless import run_headless_worker
from dgov.worktree import (
    create_worktree,
    prepare_worktree,
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
        self._settlement_flow = SettlementFlow(
            session_root=session_root,
            plan_name=dag.name,
            project_config=self.project_config,
            executor=self._executor,
        )

        if restart:
            reset_plan_state(session_root, dag.name)
        else:
            self._run_recovery_pipeline(continue_failed=continue_failed)

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
            "task_abandoned",
            "cleanup",
            plan_name=self.dag.name,
            task_slug=slug,
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
            "dag_task_governor_resumed",
            "runner",
            plan_name=self.dag.name,
            task_slug=slug,
            action=GovernorAction.RETRY.value,
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
            self._apply_rehydrate_event(ev)

    def _apply_rehydrate_event(self, ev: dict[str, Any]) -> None:
        """Apply a single event during rehydration. Extracted for testability."""
        ename = ev["event"]
        task_slug = ev.get("task_slug")
        pane = ev["pane"]

        if not task_slug or task_slug not in self.kernel.task_states:
            return

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
            self.kernel.handle(TaskMergeDone(task_slug, error=ev.get("error", "unknown error")))
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
                update_runtime_artifact_state(
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
        task = self.dag.tasks[action.task_slug]

        # Read-only roles (researcher, reviewer) produce no code changes.
        # Auto-pass review — their output is in the event log, not the worktree.
        if task.role in ("researcher", "reviewer"):
            emit_event(
                self.session_root,
                "review_pass",
                action.pane_slug,
                plan_name=self.dag.name,
                task_slug=action.task_slug,
                verdict="read_only",
            )
            return self.kernel.handle(
                TaskReviewDone(
                    action.task_slug,
                    passed=True,
                    verdict="read_only",
                    commit_count=0,
                )
            )

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
            pane_slug=action.pane_slug,
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
        task_scope: Mapping[str, object],
        on_exit: Callable[[str, str, int, str], None],
        timeout_s: int,
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

    def _build_reviewer_prompt(self, task_slug: str, task: DagTaskSpec) -> str:
        """Build a reviewer prompt with dependency diffs auto-injected."""
        import subprocess as sp

        from dgov import deploy_log

        sections: list[str] = []
        sections.append(
            "Review the following changes for semantic correctness.\n"
            "Focus on: logic errors, no-ops, silently wrong behavior, "
            "missing edge cases, and whether the code matches its stated intent.\n"
        )

        records = deploy_log.read(self.session_root, self.dag.name)
        sha_by_unit = {r.unit: r.sha for r in records}

        for dep_slug in task.depends_on:
            dep_task = self.dag.tasks.get(dep_slug)
            if not dep_task:
                continue
            sha = sha_by_unit.get(dep_slug)
            if not sha:
                sections.append(f"## {dep_slug}\nNo deploy record found (not yet merged).\n")
                continue

            diff_result = sp.run(
                ["git", "show", "--stat", "--patch", sha],
                cwd=self.session_root,
                capture_output=True,
                text=True,
            )
            diff_text = diff_result.stdout if diff_result.returncode == 0 else "(diff unavailable)"

            sections.append(
                f"## Task: {dep_slug}\n"
                f"Summary: {dep_task.summary}\n"
                f"Commit: {dep_task.commit_message}\n\n"
                f"```diff\n{diff_text}\n```\n"
            )

        # Append user-provided prompt guidance if any
        if task.prompt.strip():
            sections.append(f"## Additional review guidance\n{task.prompt}\n")

        sections.append(
            "Respond via the `done` tool with your verdict as a JSON object:\n"
            '{"approved": true/false, "issues": ["issue 1", ...]}\n'
            'If approved with no issues, use: {"approved": true, "issues": []}'
        )

        return "\n".join(sections)

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
        loop = asyncio.get_running_loop()

        # Reviewer: build prompt from dependency diffs (before retry enrichment
        # so that retry context wraps the generated prompt, not replaces it)
        if task.role == "reviewer":
            prompt = self._build_reviewer_prompt(action.task_slug, task)
        else:
            prompt = task.prompt

        # Enrich prompt with prior failure context on retry
        prior_error = self._task_errors.get(action.task_slug)
        if prior_error:
            attempt = self.kernel.attempts.get(action.task_slug, 0)
            prompt = (
                f"PREVIOUS ATTEMPT ({attempt}) FAILED:\n{prior_error}\n\n"
                f"Fix the issue described above, then complete the original task.\n\n"
                f"ORIGINAL TASK:\n{prompt}"
            )

        # Pillar #3: Snapshot Isolation
        base_ref = self._base_ref_for_task(action.task_slug)
        wt = await loop.run_in_executor(
            self._executor, create_worktree, self.session_root, action.task_slug, base_ref
        )
        self._worktrees[action.task_slug] = wt

        # Re-resolve agent from project config mapping
        agent = task.agent
        if agent in self.project_config.agents:
            agent = self.project_config.agents[agent]

        try:
            if task.role not in ("researcher", "reviewer"):
                await loop.run_in_executor(
                    self._executor,
                    partial(
                        prepare_worktree,
                        wt,
                        language=self.project_config.language,
                        setup_cmd=self.project_config.setup_cmd,
                        timeout_s=self.project_config.bootstrap_timeout,
                    ),
                )
            pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
            self._pending_dispatches.add(action.task_slug)
            self._task_start_times[action.task_slug] = time.time()
            task_scope = {
                "task_slug": action.task_slug,
                "create": list(task.files.create),
                "edit": list(task.files.edit),
                "delete": list(task.files.delete),
                "touch": list(task.files.touch),
                "read": list(task.files.read),
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
                    task_scope,
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
            self._task_errors[action.task_slug] = str(exc)
            self._pending_dispatches.discard(action.task_slug)
            try:
                await loop.run_in_executor(self._executor, remove_worktree, self.session_root, wt)
            except Exception as cleanup_exc:
                logger.warning("Worktree cleanup after dispatch failure: %s", cleanup_exc)
            self._worktrees.pop(action.task_slug, None)
            raise

    async def _prepare_and_commit(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[str | None, bool]:
        """Autofix, commit, and handle read-only roles."""
        from dgov import deploy_log

        return await self._settlement_flow.prepare_and_commit(
            task=self.dag.tasks[action.task_slug],
            action=action,
            wt=wt,
            loop=loop,
            emit_event_fn=emit_event,
            autofix_fn=autofix_sandbox,
            commit_fn=commit_in_worktree,
            deploy_append_fn=deploy_log.append,
        )

    async def _run_isolated_validation(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[str | None, IntegrationRiskRecord | None]:
        """Compute risk and run isolated validation gate."""
        return await self._settlement_flow.run_isolated_validation(
            task=self.dag.tasks[action.task_slug],
            action=action,
            wt=wt,
            loop=loop,
            emit_event_fn=emit_event,
            validate_fn=validate_sandbox,
        )

    async def _run_semantic_gate_on_candidate(
        self,
        action: MergeTask,
        wt: Worktree,
        candidate_result: IntegrationCandidateResult,
        risk_record: IntegrationRiskRecord,
        loop: asyncio.AbstractEventLoop,
    ) -> str | None:
        """Run deterministic Python semantic gate on the integrated candidate."""
        return await self._settlement_flow.run_semantic_gate_on_candidate(
            action=action,
            wt=wt,
            candidate_result=candidate_result,
            risk_record=risk_record,
            loop=loop,
            emit_event_fn=emit_event,
            remove_candidate_fn=remove_integration_candidate,
            semantic_gate_fn=run_python_semantic_gate_in_subprocess,
            rejected_emit_fn=emit_semantic_gate_rejected,
        )

    async def _validate_and_finalize_candidate(
        self,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        task_config: ProjectConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> str | None:
        """Validate candidate with same gates as isolated validation."""
        return await self._settlement_flow.validate_and_finalize_candidate(
            action=action,
            candidate_result=candidate_result,
            task_config=task_config,
            loop=loop,
            emit_event_fn=emit_event,
            validate_fn=validate_sandbox,
            remove_candidate_fn=remove_integration_candidate,
            failed_emit_fn=emit_integration_candidate_failed,
            passed_emit_fn=emit_integration_candidate_passed,
        )

    async def _create_integration_candidate_with_emit(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> IntegrationCandidateResult:
        """Create integration candidate and emit failure event if replay fails."""
        return await self._settlement_flow.create_integration_candidate_with_emit(
            action=action,
            wt=wt,
            loop=loop,
            emit_event_fn=emit_event,
            create_candidate_fn=create_integration_candidate,
            failed_emit_fn=emit_integration_candidate_failed,
        )

    async def _finalize_merge(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Merge worktree into main and record deployment."""
        from dgov import deploy_log

        await self._settlement_flow.finalize_merge(
            action=action,
            wt=wt,
            loop=loop,
            merge_fn=merge_worktree,
            deploy_append_fn=deploy_log.append,
        )

    async def _cleanup_rejected_candidate(
        self,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        verdict: IntegrationCandidateVerdict,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Clean up rejected candidate and emit failure event."""
        await self._settlement_flow.cleanup_rejected_candidate(
            action=action,
            candidate_result=candidate_result,
            verdict=verdict,
            loop=loop,
            emit_event_fn=emit_event,
            remove_candidate_fn=remove_integration_candidate,
            failed_emit_fn=emit_integration_candidate_failed,
        )

    async def _cleanup_passed_candidate(
        self,
        action: MergeTask,
        candidate_result: IntegrationCandidateResult,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Clean up passed candidate and emit success event."""
        await self._settlement_flow.cleanup_passed_candidate(
            action=action,
            candidate_result=candidate_result,
            loop=loop,
            emit_event_fn=emit_event,
            remove_candidate_fn=remove_integration_candidate,
            passed_emit_fn=emit_integration_candidate_passed,
        )

    async def _settle_and_merge(
        self,
        action: MergeTask,
        wt: Worktree,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[str | None, bool]:
        """Run settlement phases: prepare → validate → candidate → merge.

        Phase-split for testability: each phase can fail independently.
        Returns (error, was_settlement) tuple.
        """
        task = self.dag.tasks[action.task_slug]
        pc = self.project_config
        task_config = replace(pc, test_cmd=task.test_cmd) if task.test_cmd else pc

        # Phase 1: Prepare and commit (or handle read-only roles)
        error, was_settlement = await self._prepare_and_commit(action, wt, loop)
        if error or was_settlement is False:
            return error, was_settlement

        # Phase 2: Isolated validation (compute risk + run gate)
        error, risk_record = await self._run_isolated_validation(action, wt, loop)
        if error or risk_record is None:
            return error, True

        # Phase 3: Create integration candidate
        candidate_result = await self._create_integration_candidate_with_emit(action, wt, loop)
        if not candidate_result.passed:
            return candidate_result.error or "Integration candidate replay failed", True

        # Phase 4: Run semantic gate on candidate
        error = await self._run_semantic_gate_on_candidate(
            action, wt, candidate_result, risk_record, loop
        )
        if error:
            return error, True

        # Phase 5: Validate candidate with same gates as isolated validation
        error = await self._validate_and_finalize_candidate(
            action, candidate_result, task_config, loop
        )
        if error:
            return error, True

        # Phase 6: Final merge and deploy
        await self._finalize_merge(action, wt, loop)
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
            test_cmd=task.test_cmd,
        )
        retry_scope = {
            "task_slug": action.task_slug,
            "create": list(task.files.create),
            "edit": list(task.files.edit),
            "delete": list(task.files.delete),
            "touch": list(task.files.touch),
            "read": list(task.files.read),
        }

        pane_slug = action.pane_slug + "-retry"

        def _noop_exit(slug: str, pane: str, code: int, err: str = "") -> None:
            if code != 0:
                logger.warning("Settlement retry worker exited %d for %s: %s", code, slug, err)

        await run_headless_worker(
            self.session_root,
            self.dag.name,
            action.task_slug,
            pane_slug,
            wt.path,
            retry_task,
            retry_scope,
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

        # Sync terminal artifact snapshot for debugging and cleanup surfaces.
        db_state = TaskState.MERGED if not error else TaskState.FAILED
        try:
            update_runtime_artifact_state(
                self.session_root,
                action.task_slug,
                db_state.value,
                force=True,
            )
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
