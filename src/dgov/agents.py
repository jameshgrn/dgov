# Agent registry and launch commands
"""Agent registry and launch command builder.

Built-in agents: claude, codex, gemini.
Users add custom agents via TOML config files.
"""

from __future__ import annotations

import random
import shutil
import string
import time
import tomllib
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
    health_check: str | None = None
    health_fix: str | None = None
    max_concurrent: int | None = None
    color: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    source: str = "built-in"


# Built-in agents: only public CLIs that dgov ships defaults for.
_BUILTIN_AGENTS: dict[str, AgentDef] = {
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
        color=39,
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
        color=214,
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
        color=135,
    ),
}

# Module-level convenience alias (populated on first load_registry call or from built-ins).
AGENT_REGISTRY: dict[str, AgentDef] = dict(_BUILTIN_AGENTS)


def _agent_def_from_toml(agent_id: str, table: dict, source: str) -> AgentDef:
    """Build an AgentDef from a TOML [agents.X] table."""
    permissions = table.pop("permissions", {})
    resume_section = table.pop("resume", {})
    env_section = table.pop("env", {})
    return AgentDef(
        id=agent_id,
        name=table.get("name", agent_id),
        short_label=table.get("short_label", agent_id[:2]),
        prompt_command=table["command"],
        prompt_transport=table["transport"],
        prompt_option=table.get("prompt_option"),
        no_prompt_command=table.get("no_prompt_command"),
        permission_flags=dict(permissions),
        send_keys_pre_prompt=tuple(table.get("send_keys_pre_prompt", ())),
        send_keys_submit=tuple(table.get("send_keys_submit", ("Enter",))),
        send_keys_post_paste_delay_ms=table.get("send_keys_post_paste_delay_ms", 0),
        send_keys_ready_delay_ms=table.get("send_keys_ready_delay_ms", 0),
        default_flags=table.get("default_flags", ""),
        resume_template=resume_section.get("template") or table.get("resume_template"),
        health_check=table.get("health_check"),
        health_fix=table.get("health_fix"),
        max_concurrent=table.get("max_concurrent"),
        color=table.get("color"),
        env=dict(env_section),
        source=source,
    )


def _merge_agent_def(base: AgentDef, overrides: dict, source: str) -> AgentDef:
    """Merge TOML overrides onto an existing AgentDef, producing a new one."""
    permissions = overrides.pop("permissions", None)
    resume_section = overrides.pop("resume", None)
    env_section = overrides.pop("env", None)

    kwargs: dict = {}
    for f in AgentDef.__dataclass_fields__:
        if f == "source":
            kwargs["source"] = source
            continue
        if f == "permission_flags":
            kwargs[f] = dict(permissions) if permissions is not None else base.permission_flags
            continue
        if f == "resume_template":
            if resume_section and "template" in resume_section:
                kwargs[f] = resume_section["template"]
            elif "resume_template" in overrides:
                kwargs[f] = overrides["resume_template"]
            else:
                kwargs[f] = base.resume_template
            continue
        if f == "env":
            if env_section is not None:
                merged_env = dict(base.env)
                merged_env.update(env_section)
                kwargs[f] = merged_env
            else:
                kwargs[f] = base.env
            continue
        # Map TOML key names to dataclass field names
        toml_key = {
            "prompt_command": "command",
            "prompt_transport": "transport",
        }.get(f, f)
        if toml_key in overrides:
            kwargs[f] = overrides[toml_key]
        elif f in overrides:
            kwargs[f] = overrides[f]
        else:
            kwargs[f] = getattr(base, f)

    # Handle tuple fields
    for tf in ("send_keys_pre_prompt", "send_keys_submit"):
        if tf in kwargs and isinstance(kwargs[tf], list):
            kwargs[tf] = tuple(kwargs[tf])

    return AgentDef(**kwargs)


def _load_toml_file(path: Path) -> dict[str, dict]:
    """Load agents from a TOML file. Returns {agent_id: table_dict}."""
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("agents", {})


def load_registry(project_root: str | None = None) -> dict[str, AgentDef]:
    """Load agent registry: built-ins → user global → project-local.

    Each layer merges over the previous. New agent IDs are added;
    existing IDs get field-level overrides.
    """
    registry = dict(_BUILTIN_AGENTS)

    # User global: ~/.dgov/agents.toml
    user_config = Path.home() / ".dgov" / "agents.toml"
    for agent_id, table in _load_toml_file(user_config).items():
        table = dict(table)  # shallow copy so pops don't mutate cache
        if agent_id in registry:
            registry[agent_id] = _merge_agent_def(registry[agent_id], table, "user")
        else:
            registry[agent_id] = _agent_def_from_toml(agent_id, table, "user")

    # Project-local: <project_root>/.dgov/agents.toml
    if project_root:
        project_config = Path(project_root) / ".dgov" / "agents.toml"
        for agent_id, table in _load_toml_file(project_config).items():
            table = dict(table)
            if agent_id in registry:
                registry[agent_id] = _merge_agent_def(registry[agent_id], table, "project")
            else:
                registry[agent_id] = _agent_def_from_toml(agent_id, table, "project")

    return registry


def detect_installed_agents(
    registry: dict[str, AgentDef] | None = None,
) -> list[str]:
    """Return IDs of agent CLIs found on PATH."""
    reg = registry or AGENT_REGISTRY
    return [
        agent_id for agent_id, defn in reg.items() if shutil.which(defn.prompt_command.split()[0])
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
    """Shell snippet that reads prompt from file into $DGOV_PROMPT_CONTENT, then deletes file."""
    return (
        f'DGOV_PROMPT_FILE="{filepath}"; '
        f'DGOV_PROMPT_CONTENT="$(cat "$DGOV_PROMPT_FILE")"; '
        f'rm -f "$DGOV_PROMPT_FILE"'
    )


def build_launch_command(
    agent_id: str,
    prompt: str | None,
    permission_mode: str = "",
    *,
    project_root: str = ".",
    slug: str = "task",
    extra_flags: str = "",
    registry: dict[str, AgentDef] | None = None,
) -> str:
    """Build the shell command to launch an agent with an optional prompt.

    For positional and option transports, writes prompt to a temp file
    and builds a shell snippet that reads+deletes it (avoids escaping issues).

    Returns the full shell command string. For send-keys transport agents,
    returns just the base command (prompt delivered separately via tmux buffer).
    """
    reg = registry or AGENT_REGISTRY
    agent = reg[agent_id]
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
        return f"{snippet}; printf '%s\\n' \"$DGOV_PROMPT_CONTENT\" | {base}"

    if agent.prompt_transport == "option" and agent.prompt_option:
        return f'{snippet}; {base} {agent.prompt_option} "$DGOV_PROMPT_CONTENT"'

    # positional
    return f'{snippet}; {base} "$DGOV_PROMPT_CONTENT"'


def build_resume_command(
    agent_id: str,
    permission_mode: str = "",
    registry: dict[str, AgentDef] | None = None,
) -> str | None:
    """Build command to resume the last session for an agent."""
    reg = registry or AGENT_REGISTRY
    agent = reg[agent_id]
    if not agent.resume_template:
        return None
    flags = _perm_flags(agent, permission_mode)
    suffix = f" {flags}" if flags else ""
    return agent.resume_template.replace("{permissions}", suffix)
