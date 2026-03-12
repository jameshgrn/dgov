"""Tmux-based WorkerBackend for distributary scheduler integration.

Spawns workers as tmux panes via the workstation agent registry,
polls done signals + output stabilization for completion detection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dgov.models import TaskSpec
from dgov.panes import (
    _is_done,
    capture_worker_output,
    close_worker_pane,
    create_worker_pane,
)


@dataclass
class PaneHandle:
    slug: str
    project_root: str


class TmuxWorkerBackend:
    """WorkerBackend that spawns workers as tmux panes.

    Maps TaskSpec fields to workstation agent registry:
    - task.worker_cmd = "claude" → agent="claude"
    - task.provider = "river-gpu0" → agent="pi" with provider flag
    - Default: agent="pi"
    """

    def __init__(
        self,
        session_root: str,
        worktree_dir: str = ".workstation/worktrees",
        stable_threshold: int = 15,
        poll_interval: int = 3,
    ):
        self.session_root = session_root
        self.worktree_dir = worktree_dir
        self.stable_threshold = stable_threshold
        self.poll_interval = poll_interval

    def _resolve_agent(self, task: TaskSpec) -> tuple[str, str]:
        """Return (agent_name, extra_flags) from task spec."""
        if task.worker_cmd:
            cmd = task.worker_cmd.strip().split()[0]
            if cmd in ("claude", "codex", "gemini", "pi"):
                return cmd, ""
            return cmd, ""
        if task.provider:
            return "pi", f"--provider {task.provider}"
        return "pi", ""

    @staticmethod
    def _extract_slug(handle: Any) -> str:
        if isinstance(handle, PaneHandle):
            return handle.slug
        if isinstance(handle, str):
            return handle
        raise TypeError(f"Unsupported backend handle type: {type(handle).__name__}")

    async def spawn(self, task: TaskSpec, worktree_path: Path, env: dict[str, str]) -> Any:
        """Create a tmux pane for the task. Return pane handle."""
        agent, extra_flags = self._resolve_agent(task)

        # Determine project_root from worktree_path parent convention
        # .workstation/worktrees/{slug} → project_root is 3 levels up
        project_root = str(worktree_path.parent.parent.parent)

        pane_kwargs: dict[str, Any] = {}
        # Reuse an existing worktree only when a caller pre-created it.
        # Batch mode passes a planned path that often doesn't exist yet.
        if worktree_path.exists():
            pane_kwargs["existing_worktree"] = str(worktree_path)

        pane = await asyncio.to_thread(
            create_worker_pane,
            project_root=project_root,
            prompt=task.body or task.description,
            agent=agent,
            permission_mode=task.permission_mode,
            slug=task.id,
            extra_flags=extra_flags,
            session_root=self.session_root,
            env_vars={k: v for k, v in env.items() if k.startswith("DISTRIBUTARY_")},
            **pane_kwargs,
        )
        return PaneHandle(slug=pane.slug, project_root=project_root)

    async def wait(self, handle: Any, timeout: int) -> bool:
        """Poll done signal + output stabilization."""
        slug = self._extract_slug(handle)
        start = asyncio.get_event_loop().time()
        last_output: str | None = None
        stable_since: float | None = None

        while asyncio.get_event_loop().time() - start < timeout:
            # Check done signal
            if await asyncio.to_thread(_is_done, self.session_root, slug):
                return True

            # Check output stabilization
            output = await asyncio.to_thread(
                capture_worker_output,
                self.session_root,
                slug,
                lines=20,
                session_root=self.session_root,
            )
            if output is not None:
                if output == last_output:
                    if stable_since is None:
                        stable_since = asyncio.get_event_loop().time()
                    elif asyncio.get_event_loop().time() - stable_since >= self.stable_threshold:
                        return True
                else:
                    last_output = output
                    stable_since = None

            await asyncio.sleep(self.poll_interval)

        return False

    async def cleanup(self, handle: Any) -> None:
        """Close the tmux pane."""
        slug = self._extract_slug(handle)
        project_root = handle.project_root if isinstance(handle, PaneHandle) else self.session_root
        await asyncio.to_thread(
            close_worker_pane,
            project_root,
            slug,
            session_root=self.session_root,
        )
