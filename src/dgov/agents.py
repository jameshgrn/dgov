"""Minimal agent definitions — lookup table only.

This is a drastically simplified version of the original agents.py.
No registry, no caching, no health checks, no mtime tracking.
Just a lookup table from agent ID to launch command.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HealthDef:
    """Health check configuration."""

    check: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class AgentDef:
    """Minimal agent definition."""

    id: str
    name: str
    # Command template with {prompt} placeholder
    command_template: str
    # Delay after pane creation before sending command (ms)
    ready_delay_ms: int = 1000
    # Health check configuration
    health: HealthDef = field(default_factory=lambda: HealthDef())
    # Maximum concurrent workers for this agent
    max_concurrent: int | None = None


# Built-in agent definitions — THE ONLY SOURCE OF TRUTH
AGENT_REGISTRY: dict[str, AgentDef] = {
    "mock": AgentDef(
        id="mock",
        name="Mock Agent",
        command_template="echo 'MOCK: {prompt}' && sleep 2 && echo 'MOCK: Done'",
        ready_delay_ms=500,
    ),
    "kimi-k25-0": AgentDef(
        id="kimi-k25-0",
        name="Kimi K2.5 Worker 0",
        command_template="pi -p --provider fireworks --model accounts/fireworks/routers/kimi-k2p5-turbo --thinking off --temperature 0.7 --timeout 600 --prompt {prompt}",
        ready_delay_ms=2000,
    ),
    "kimi-k25-1": AgentDef(
        id="kimi-k25-1",
        name="Kimi K2.5 Worker 1",
        command_template="pi -p --provider fireworks --model accounts/fireworks/routers/kimi-k2p5-turbo --thinking off --temperature 0.7 --timeout 600 --prompt {prompt}",
        ready_delay_ms=2000,
    ),
    "kimi-k25-2": AgentDef(
        id="kimi-k25-2",
        name="Kimi K2.5 Worker 2",
        command_template="pi -p --provider fireworks --model accounts/fireworks/routers/kimi-k2p5-turbo --thinking off --temperature 0.7 --timeout 600 --prompt {prompt}",
        ready_delay_ms=2000,
    ),
    "kimi-k25-3": AgentDef(
        id="kimi-k25-3",
        name="Kimi K2.5 Worker 3",
        command_template="pi -p --provider fireworks --model accounts/fireworks/routers/kimi-k2p5-turbo --thinking off --temperature 0.7 --timeout 600 --prompt {prompt}",
        ready_delay_ms=2000,
    ),
    "kimi-k25-4": AgentDef(
        id="kimi-k25-4",
        name="Kimi K2.5 Worker 4",
        command_template="pi -p --provider fireworks --model accounts/fireworks/routers/kimi-k2p5-turbo --thinking off --temperature 0.7 --timeout 600 --prompt {prompt}",
        ready_delay_ms=2000,
    ),
}


def get_registry(project_root: str | None = None) -> dict[str, AgentDef]:
    """Get agent registry (minimal version - no caching).

    Returns the built-in agents. Custom configs not supported in minimal version.
    """
    return dict(AGENT_REGISTRY)


def get_agent(agent_id: str) -> AgentDef | None:
    """Get agent definition by ID."""
    return AGENT_REGISTRY.get(agent_id)


def build_launch_command(
    agent_id: str,
    worktree: str | None = None,
    git_env: dict[str, str] | None = None,
    prompt: str = "",
    pane_id: str = "",
    permissions: str = "acceptEdits",
    # New API parameters (for runner compatibility)
    project_root: str | None = None,
    slug: str | None = None,
    registry: dict | None = None,
    permission_mode: str | None = None,
    force_headless: bool = False,
    session_dir: str | None = None,
) -> str | None:
    """Build shell command to launch agent in worktree.

    Supports two calling conventions:
    1. Legacy: build_launch_command(agent_id, worktree, git_env, prompt, pane_id, permissions)
    2. Runner: build_launch_command(agent_id, prompt=..., project_root=..., slug=..., ...)

    Returns None if agent_id not found.
    """
    agent = get_agent(agent_id)
    if agent is None:
        return None

    # Escape prompt for shell
    escaped_prompt = shlex.quote(prompt)

    # Substitute into command template
    cmd = agent.command_template.format(prompt=escaped_prompt)

    # For pi agents, inject session isolation controls
    if session_dir and cmd.startswith("pi "):
        # Insert --session-dir right after 'pi' (and -p if present)
        parts = cmd.split(None, 2)  # Split on whitespace, max 2 splits
        if len(parts) >= 2:
            base = f"{parts[0]} {parts[1]}"
            rest = parts[2] if len(parts) > 2 else ""
            cmd = f"{base} --session-dir {shlex.quote(session_dir)} {rest}"
        else:
            cmd = f"{parts[0]} --session-dir {shlex.quote(session_dir)}"

    # Determine effective worktree path and make it absolute
    from pathlib import Path

    effective_worktree = Path(project_root or worktree or ".").absolute()

    # Build git environment if provided
    env_vars = ""
    if git_env:
        env_vars = " ".join(f"{k}={shlex.quote(v)}" for k, v in git_env.items())
        if env_vars:
            env_vars += " "

    return f"cd {shlex.quote(str(effective_worktree))} && {env_vars}{cmd}"


def list_agents() -> list[str]:
    """List available agent IDs."""
    return list(AGENT_REGISTRY.keys())
