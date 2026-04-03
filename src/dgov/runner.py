"""Async bridge for DagKernel — event-driven, zero-latency via pipe.

Follows sentrux principles:
- Events are source of truth
- Zero-latency signaling (no polling)
- Reader opens pipe FIRST, then writers are spawned
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dgov.dag_parser import DagDefinition, DagTaskSpec
from dgov.kernel import (
    DagDone,
    DagKernel,
    DispatchTask,
    GovernorAction,
    InterruptGovernor,
    MergeTask,
    TaskDispatched,
    TaskGovernorResumed,
    TaskMergeDone,
    TaskWaitDone,
    TaskReviewDone,
    CloseTask,
)
from dgov.persistence import emit_event
from dgov.tmux import create_background_pane, send_prompt_via_buffer, set_pane_option
from dgov.types import PaneState
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
    """Event-driven DAG runner — named pipe for zero-latency signaling."""

    def __init__(self, dag: DagDefinition, session_root: str = "."):
        self.dag = dag
        self.session_root = session_root
        self.deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}
        self.kernel = DagKernel(deps=self.deps)
        self._pending_dispatches: set[str] = set()
        self._event_queue: asyncio.Queue[WorkerExit] = asyncio.Queue()
        self._executor: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_reader = threading.Event()
        self._worktrees: dict[str, Worktree] = {}

    async def run(self) -> dict[str, str]:
        """Execute DAG with event-driven state machine."""
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._loop = asyncio.get_event_loop()

        pipe_path = await self._setup_event_pipe()

        try:
            return await self._run_with_pipe(pipe_path)
        finally:
            self._cleanup_event_pipe(pipe_path)
            self._executor.shutdown(wait=False)

    async def _setup_event_pipe(self) -> Path:
        """Create named pipe and start reader."""
        pipe_path = Path(self.session_root) / ".dgov" / "event.pipe"
        pipe_path.parent.mkdir(parents=True, exist_ok=True)
        if pipe_path.exists():
            pipe_path.unlink()

        os.mkfifo(str(pipe_path))

        assert self._executor is not None
        self._executor.submit(self._pipe_reader_loop, pipe_path)
        await asyncio.sleep(0.2)

        return pipe_path

    def _pipe_reader_loop(self, pipe_path: Path) -> None:
        """Blocking loop that reads from pipe and puts events on queue."""
        try:
            fd = os.open(str(pipe_path), os.O_RDONLY | os.O_NONBLOCK)

            while not self._stop_reader.is_set():
                import select

                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue

                try:
                    data = os.read(fd, 4096).decode()
                except BlockingIOError:
                    continue

                if not data:
                    continue

                for line in data.strip().split("\n"):
                    if not line:
                        continue

                    try:
                        payload = json.loads(line)
                        exit_event = WorkerExit(
                            task_slug=payload["task_slug"],
                            pane_slug=payload["pane_slug"],
                            exit_code=payload["exit_code"],
                            output_dir=str(
                                Path(self.session_root) / ".dgov" / "out" / payload["task_slug"]
                            ),
                        )
                        self._loop.call_soon_threadsafe(self._event_queue.put_nowait, exit_event)
                    except (json.JSONDecodeError, KeyError) as exc:
                        logger.warning("Invalid event: %s", exc)

            os.close(fd)

        except OSError as exc:
            logger.debug("Reader: pipe error: %s", exc)

    async def _run_with_pipe(self, pipe_path: Path) -> dict[str, str]:
        """Main event loop — dispatches workers, waits for pipe events."""
        actions = self.kernel.start()

        while True:
            dispatch_coros = []
            next_actions = []

            for action in actions:
                if isinstance(action, DispatchTask):
                    dispatch_coros.append(self._dispatch(action, pipe_path))
                elif isinstance(action, MergeTask):
                    dispatch_coros.append(self._merge(action))
                elif isinstance(action, InterruptGovernor):
                    logger.info("Governor interrupt: %s (auto-retry)", action.reason)
                    # For now, auto-retry immediately
                    next_actions.extend(
                        self.kernel.handle(
                            TaskGovernorResumed(action.task_slug, GovernorAction.RETRY)
                        )
                    )
                elif isinstance(action, DagDone):
                    return {slug: state.value for slug, state in self.kernel.task_states.items()}

            if dispatch_coros:
                await asyncio.gather(*dispatch_coros)

            # If the dispatch or merge phase produced new immediate actions, loop again
            if next_actions:
                actions = next_actions
                continue

            if self.kernel.done:
                break

            try:
                # Wait for next worker to finish
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
                review_actions = [a for a in actions if a.__class__.__name__ == "ReviewTask"]
                for ra in review_actions:
                    actions = self.kernel.handle(
                        TaskReviewDone(
                            ra.task_slug, passed=True, verdict="auto-pass", commit_count=1
                        )
                    )

            except asyncio.TimeoutError:
                if not self._pending_dispatches and self.kernel.done:
                    break
                # If nothing's pending but kernel isn't done, we might have stalled
                # Check for new actions from the kernel state
                actions = self.kernel.handle(TaskWaitDone("", "", "")) if not actions else actions
                continue

        return {slug: state.value for slug, state in self.kernel.task_states.items()}

    async def _merge(self, action: MergeTask) -> None:
        """Commit-or-Kill: Merge worktree branch into base (Pillar #2)."""
        wt = self._worktrees.get(action.task_slug)
        if not wt:
            # Fallback for Tmux or tasks without explicit worktree (though headless now always has one)
            self.kernel.handle(TaskMergeDone(action.task_slug))
            return

        try:
            # 1. Commit everything in the sandbox
            task = self.dag.tasks[action.task_slug]
            msg = task.commit_message or f"feat: completed {action.task_slug}"
            
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                commit_in_worktree,
                wt,
                msg
            )

            # 2. Merge to Main (Atomic)
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                merge_worktree,
                self.session_root,
                wt
            )

            # 3. Cleanup Sandbox (Pillar #10)
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                remove_worktree,
                self.session_root,
                wt
            )
            
            error = None
        except Exception as exc:
            logger.error("Merge failed for %s: %s", action.task_slug, exc)
            error = str(exc)

        self.kernel.handle(TaskMergeDone(action.task_slug, error=error))

    async def _dispatch(self, action: DispatchTask, pipe_path: Path) -> None:
        """Dispatch task to worker (Headless or Tmux)."""
        task = self.dag.tasks[action.task_slug]
        output_dir = Path(self.session_root) / ".dgov" / "out" / action.task_slug
        output_dir.mkdir(parents=True, exist_ok=True)

        # Decision: Headless if fireworks/ model, else Tmux
        use_headless = task.agent and (
            task.agent.startswith("fireworks/") or task.agent.startswith("accounts/")
        )

        if use_headless:
            # Pillar #9: Zero-latency signaling via async subprocess
            import uuid

            # Pillar #3: Snapshot Isolation via dedicated worktree
            wt = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                create_worktree,
                self.session_root,
                action.task_slug
            )
            self._worktrees[action.task_slug] = wt

            pane_slug = f"headless-{action.task_slug}-{uuid.uuid4().hex[:8]}"
            self._pending_dispatches.add(action.task_slug)

            # Atomic transition to WAITING MUST happen before we spawn the task
            self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

            emit_event(
                self.session_root,
                "dag_task_dispatched",
                pane_slug,
                task_slug=action.task_slug,
                agent=task.agent,
            )

            asyncio.create_task(
                self._run_headless_worker(
                    action.task_slug, pane_slug, wt.path, task, output_dir, pipe_path
                )
            )
        else:
            pane_slug = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                self._spawn_worker,
                action.task_slug,
                task,
                output_dir,
                pipe_path,
            )
            self._pending_dispatches.add(action.task_slug)
            self.kernel.handle(TaskDispatched(action.task_slug, pane_slug))

            emit_event(
                self.session_root,
                "dag_task_dispatched",
                pane_slug,
                task_slug=action.task_slug,
                agent=task.agent,
            )

    async def _run_headless_worker(
        self,
        task_slug: str,
        pane_slug: str,
        worktree_path: Path,
        task: DagTaskSpec,
        output_dir: Path,
        pipe_path: Path,
    ) -> None:
        """Async worker lifecycle (Pillar #2: Atomic Attempt)."""
        import sys

        # Pillar #10: Use current executable to bypass 'uv run' overhead
        cmd = [
            sys.executable,
            "src/dgov/worker.py",
            "--goal",
            task.prompt,
            "--worktree",
            str(worktree_path),
            "--model",
            task.agent,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.session_root,
        )

        # Pillar #6: Async event stream processing
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode().strip()
            try:
                data = json.loads(line)
                if "worker_event" in data:
                    ev = data["worker_event"]
                    # Map worker events to journal (Pillar #9)
                    emit_event(
                        self.session_root,
                        f"worker_{ev['type']}",
                        pane_slug,
                        task_slug=task_slug,
                        content=ev.get("content"),
                    )
            except json.JSONDecodeError:
                if line:
                    logger.debug("Worker raw: %s", line)

        exit_code = await process.wait()

        # Pillar #10: Unify signaling — direct queue injection for headless
        exit_event = WorkerExit(
            task_slug=task_slug,
            pane_slug=pane_slug,
            exit_code=exit_code,
            output_dir=str(output_dir),
        )
        self._loop.call_soon_threadsafe(self._event_queue.put_nowait, exit_event)

    def _spawn_worker(
        self,
        task_slug: str,
        task: DagTaskSpec,
        output_dir: Path,
        pipe_path: Path,
    ) -> str:
        """Spawn tmux worker (sync, runs in thread)."""
        import uuid

        pane_slug = f"{task_slug}-{uuid.uuid4().hex[:8]}"

        # Setup tmux pane
        create_background_pane(pane_slug)
        set_pane_option(pane_slug, "remain-on-exit", "on")

        # Send launch command
        # Logic: cd to worktree, run agent, then signal pipe
        # For now, we cd to session_root
        launch_cmd = f"cd {self.session_root} && {task.prompt}"

        # This is the "signal" part that Tmux workers must do manually
        signal_cmd = f'echo \'{{"task_slug": "{task_slug}", "pane_slug": "{pane_slug}", "exit_code": 0}}\' > {pipe_path}'

        send_prompt_via_buffer(pane_slug, f"{launch_cmd} && {signal_cmd}")

        return pane_slug

    def _cleanup_event_pipe(self, pipe_path: Path) -> None:
        """Clean up named pipe and stop reader."""
        self._stop_reader.set()
        if pipe_path.exists():
            try:
                # Wake up the select() in reader thread
                with open(pipe_path, "w") as f:
                    f.write("\n")
            except Exception:
                pass
            pipe_path.unlink()
