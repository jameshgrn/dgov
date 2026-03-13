# Agent registry and launch commands
"""Agent registry and launch command builder.

Mirrors dmux's AGENT_REGISTRY from agentLaunch.js but in Python.
Only includes agents Jake actually uses on this workstation.
"""

from __future__ import annotations

import random
import shutil
import string
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    short_label: str
    prompt_command: str
    prompt_transport: str  # "positional" | "option" | "send-keys" | "stdin"
    prompt_option: str | None = None
    no_prompt_command: str | None = None
    permission_flags: dict[str, str] = field(default_factory=dict)
    send_keys_pre_prompt: tuple[str, ...] = ()
    send_keys_submit: tuple[str, ...] = ("Enter",)
    send_keys_post_paste_delay_ms: int = 0
    send_keys_ready_delay_ms: int = 0
    default_flags: str = ""
    resume_template: str | None = None


# Maps agent IDs to their definitions.
AGENT_REGISTRY: dict[str, AgentDef] = {
    "claude": AgentDef(
        id="claude",
        name="Claude Code",
        short_label="cc",
        prompt_command="claude",
        prompt_transport="positional",
        permission_flags={
            "plan": "--permission-mode plan",
            "acceptEdits": "--permission-mode acceptEdits",
            "bypassPermissions": "--dangerously-skip-permissions",
        },
        resume_template="claude --continue{permissions}",
    ),
    "pi": AgentDef(
        id="pi",
        name="pi CLI",
        short_label="pi",
        prompt_command="pi",
        prompt_transport="positional",
        default_flags="--provider river-gpu0",
        permission_flags={
            "plan": "--tools read,grep,find,ls",
        },
        resume_template="pi --continue{permissions}",
    ),
    "codex": AgentDef(
        id="codex",
        name="Codex",
        short_label="cx",
        prompt_command="codex",
        prompt_transport="positional",
        permission_flags={
            "acceptEdits": "--full-auto",
            "bypassPermissions": "--dangerously-bypass-approvals-and-sandbox",
        },
        resume_template="codex resume --last{permissions}",
    ),
    "gemini": AgentDef(
        id="gemini",
        name="Gemini CLI",
        short_label="gm",
        prompt_command="gemini",
        prompt_transport="option",
        prompt_option="--prompt-interactive",
        permission_flags={
            "plan": "--approval-mode plan",
            "acceptEdits": "--approval-mode auto_edit",
            "bypassPermissions": "--approval-mode yolo",
        },
        resume_template="gemini --resume latest{permissions}",
    ),
    "qwen": AgentDef(
        id="qwen",
        name="Qwen CLI",
        short_label="qn",
        prompt_command="qwen",
        prompt_transport="option",
        prompt_option="-i",
        permission_flags={
            "plan": "--approval-mode plan",
            "acceptEdits": "--approval-mode auto-edit",
            "bypassPermissions": "--approval-mode yolo",
        },
        resume_template="qwen --continue{permissions}",
    ),
}


def detect_installed_agents() -> list[str]:
    """Return IDs of agent CLIs found on PATH."""
    return [
        agent_id
        for agent_id, defn in AGENT_REGISTRY.items()
        if shutil.which(defn.prompt_command.split()[0])
    ]


def _perm_flags(agent: AgentDef, mode: str) -> str:
    if not mode:
        return ""
    return agent.permission_flags.get(mode, "")


def _write_prompt_file(project_root: str, slug: str, prompt: str) -> str:
    """Write prompt to .dgov/prompts/<slug>--<ts>-<rand>.txt, return path."""
    prompts_dir = Path(project_root) / ".dgov" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase, k=4))
    filename = f"{slug}--{ts}-{rand}.txt"
    filepath = prompts_dir / filename
    filepath.write_text(prompt, encoding="utf-8")
    return str(filepath)


def _prompt_read_and_delete_snippet(filepath: str) -> str:
    """Shell snippet that reads prompt from file into $DMUX_PROMPT_CONTENT, then deletes file."""
    return (
        f'DMUX_PROMPT_FILE="{filepath}"; '
        f'DMUX_PROMPT_CONTENT="$(cat "$DMUX_PROMPT_FILE")"; '
        f'rm -f "$DMUX_PROMPT_FILE"'
    )


def build_launch_command(
    agent_id: str,
    prompt: str | None,
    permission_mode: str = "",
    *,
    project_root: str = ".",
    slug: str = "task",
    extra_flags: str = "",
) -> str:
    """Build the shell command to launch an agent with an optional prompt.

    For positional and option transports, writes prompt to a temp file
    and builds a shell snippet that reads+deletes it (avoids escaping issues).

    Returns the full shell command string. For send-keys transport agents,
    returns just the base command (prompt delivered separately via tmux buffer).
    """
    agent = AGENT_REGISTRY[agent_id]
    flags = _perm_flags(agent, permission_mode)
    base = agent.prompt_command
    if agent.default_flags:
        base = f"{base} {agent.default_flags}"
    if flags:
        base = f"{base} {flags}"
    if extra_flags:
        base = f"{base} {extra_flags}"

    if not prompt:
        return agent.no_prompt_command or base

    if agent.prompt_transport == "send-keys":
        return agent.no_prompt_command or base

    prompt_file = _write_prompt_file(project_root, slug, prompt)
    snippet = _prompt_read_and_delete_snippet(prompt_file)

    if agent.prompt_transport == "stdin":
        return f"{snippet}; printf '%s\\n' \"$DMUX_PROMPT_CONTENT\" | {base}"

    if agent.prompt_transport == "option" and agent.prompt_option:
        return f'{snippet}; {base} {agent.prompt_option} "$DMUX_PROMPT_CONTENT"'

    # positional
    return f'{snippet}; {base} "$DMUX_PROMPT_CONTENT"'


def build_resume_command(agent_id: str, permission_mode: str = "") -> str | None:
    """Build command to resume the last session for an agent."""
    agent = AGENT_REGISTRY[agent_id]
    if not agent.resume_template:
        return None
    flags = _perm_flags(agent, permission_mode)
    suffix = f" {flags}" if flags else ""
    return agent.resume_template.replace("{permissions}", suffix)
