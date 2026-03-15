# Agent registry and launch commands
"""Agent registry and launch command builder.

Built-in agents: claude, codex, gemini, opencode, cline, qwen, amp,
pi, cursor, copilot, crush.
Users add custom agents via TOML config files.
"""

from __future__ import annotations

import logging
import random
import shutil
import string
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DoneStrategy:
    """How to detect when an agent has finished its task.

    Strategy types:
    - "signal": Wait for done-signal file (default, current behavior).
    - "exit": Agent process exits — rely on done file + pane liveness, skip commit check.
    - "commit": Wait for new commits on branch — skip output stabilization.
    - "stable": Wait for output to stabilize for stable_seconds.
    """

    type: str  # "signal" | "exit" | "commit" | "stable"
    stable_seconds: int = 15  # only used when type="stable"


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
    max_retries: int = 0
    retry_escalate_to: str | None = None
    color: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    done_strategy: DoneStrategy | None = None
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
        prompt_command="codex exec",
        prompt_transport="positional",
        permission_flags={
            "acceptEdits": "--full-auto",
            "bypassPermissions": "--dangerously-bypass-approvals-and-sandbox",
        },
        color=214,
        done_strategy=DoneStrategy(type="exit"),
    ),
    "gemini": AgentDef(
        id="gemini",
        name="Gemini CLI",
        short_label="gm",
        prompt_command="gemini",
        prompt_transport="option",
        prompt_option="--prompt",
        permission_flags={
            "plan": "--approval-mode plan",
            "acceptEdits": "--approval-mode auto_edit",
            "bypassPermissions": "--approval-mode yolo",
        },
        resume_template="gemini --resume latest{permissions}",
        color=135,
        done_strategy=DoneStrategy(type="exit"),
    ),
    "opencode": AgentDef(
        id="opencode",
        name="OpenCode",
        short_label="oc",
        prompt_command="opencode",
        prompt_transport="option",
        prompt_option="--prompt",
        color=82,
    ),
    "cline": AgentDef(
        id="cline",
        name="Cline CLI",
        short_label="cl",
        prompt_command="cline",
        prompt_transport="send-keys",
        send_keys_post_paste_delay_ms=120,
        send_keys_ready_delay_ms=2500,
        permission_flags={
            "plan": "--plan",
            "acceptEdits": "--act",
            "bypassPermissions": "--act --yolo",
        },
        color=196,
        done_strategy=DoneStrategy(type="stable", stable_seconds=30),
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
        color=99,
    ),
    "amp": AgentDef(
        id="amp",
        name="Amp CLI",
        short_label="ap",
        prompt_command="amp",
        prompt_transport="stdin",
        permission_flags={
            "bypassPermissions": "--dangerously-allow-all",
        },
        color=208,
    ),
    "pi": AgentDef(
        id="pi",
        name="pi CLI",
        short_label="pi",
        prompt_command="pi",
        prompt_transport="positional",
        default_flags="-p",  # non-interactive: process prompt and exit
        permission_flags={
            "plan": "--tools read,grep,find,ls",
        },
        resume_template="pi --continue{permissions}",
        color=34,
        done_strategy=DoneStrategy(type="exit"),
    ),
    "cursor": AgentDef(
        id="cursor",
        name="Cursor CLI",
        short_label="cr",
        prompt_command="cursor-agent",
        prompt_transport="positional",
        color=45,
    ),
    "copilot": AgentDef(
        id="copilot",
        name="Copilot CLI",
        short_label="co",
        prompt_command="copilot",
        prompt_transport="option",
        prompt_option="-i",
        permission_flags={
            "acceptEdits": "--allow-tool write",
            "bypassPermissions": "--allow-all",
        },
        resume_template="copilot --continue{permissions}",
        color=231,
    ),
    "crush": AgentDef(
        id="crush",
        name="Crush CLI",
        short_label="cs",
        prompt_command="crush run",
        no_prompt_command="crush",
        prompt_transport="send-keys",
        send_keys_pre_prompt=("Escape", "Tab"),
        send_keys_submit=("Enter",),
        send_keys_post_paste_delay_ms=200,
        send_keys_ready_delay_ms=1200,
        permission_flags={
            "bypassPermissions": "--yolo",
        },
        color=219,
        done_strategy=DoneStrategy(type="stable", stable_seconds=30),
    ),
}

# Module-level convenience alias (populated on first load_registry call or from built-ins).
AGENT_REGISTRY: dict[str, AgentDef] = dict(_BUILTIN_AGENTS)


def _done_strategy_from_toml(table: dict) -> DoneStrategy | None:
    """Parse an optional [agents.X.done] section into a DoneStrategy."""
    done_section = table.pop("done", None)
    if not done_section:
        return None
    return DoneStrategy(
        type=done_section["type"],
        stable_seconds=done_section.get("stable_seconds", 15),
    )


def _agent_def_from_toml(agent_id: str, table: dict, source: str) -> AgentDef:
    """Build an AgentDef from a TOML [agents.X] table."""
    permissions = table.pop("permissions", {})
    resume_section = table.pop("resume", {})
    env_section = table.pop("env", {})
    done_strategy = _done_strategy_from_toml(table)
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
        max_retries=table.get("max_retries", 0),
        retry_escalate_to=table.get("retry_escalate_to"),
        color=table.get("color"),
        env=dict(env_section),
        done_strategy=done_strategy,
        source=source,
    )


def _merge_agent_def(base: AgentDef, overrides: dict, source: str) -> AgentDef:
    """Merge TOML overrides onto an existing AgentDef, producing a new one."""
    permissions = overrides.pop("permissions", None)
    resume_section = overrides.pop("resume", None)
    env_section = overrides.pop("env", None)
    done_strategy = _done_strategy_from_toml(overrides)

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
        if f == "done_strategy":
            kwargs[f] = done_strategy if done_strategy is not None else base.done_strategy
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
            # Project-local config: security boundary.
            # Cannot define shell commands (command, no_prompt_command, health_check, health_fix)
            # or override env vars. Can only select/configure existing user/global agents.
            for unsafe_key in (
                "command",
                "no_prompt_command",
                "health_check",
                "health_fix",
                "default_flags",
            ):
                table.pop(unsafe_key, None)
            if "env" in table:
                table.pop("env")
            if agent_id in registry:
                registry[agent_id] = _merge_agent_def(registry[agent_id], table, "project")
            # Project config cannot define NEW agents, only override existing ones

    return registry


def detect_installed_agents(
    registry: dict[str, AgentDef] | None = None,
) -> list[str]:
    """Return IDs of agent CLIs found on PATH."""
    reg = registry or AGENT_REGISTRY
    return [
        agent_id for agent_id, defn in reg.items() if shutil.which(defn.prompt_command.split()[0])
    ]


# Preferred fallback order when no default is configured.
_DEFAULT_AGENT_CHAIN = ("claude", "codex", "gemini")


def _load_dgov_config() -> dict:
    """Load [dgov] section from ~/.dgov/config.toml."""
    config_path = Path.home() / ".dgov" / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("dgov", {})
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Malformed TOML in %s: %s", config_path, exc)
        return {}


def _load_project_config(project_root: str) -> dict:
    """Load [dgov] section from <project_root>/.dgov/config.toml."""
    config_path = Path(project_root) / ".dgov" / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("dgov", {})
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Malformed TOML in %s: %s", config_path, exc)
        return {}


def get_governor_agent(project_root: str | None = None) -> tuple[str | None, str | None]:
    """Return (governor_agent, governor_permissions) from config.

    Priority: project-local .dgov/config.toml > user-global ~/.dgov/config.toml.
    Returns (None, None) if not configured anywhere.
    """
    global_cfg = _load_dgov_config()
    project_cfg = _load_project_config(project_root) if project_root else {}

    agent = project_cfg.get("governor_agent") or global_cfg.get("governor_agent")
    perms = project_cfg.get("governor_permissions") or global_cfg.get("governor_permissions", "")

    if not agent:
        return None, None
    return agent, perms


def write_project_config(project_root: str, key: str, value: str) -> None:
    """Update a key in [dgov] section of <project_root>/.dgov/config.toml."""
    config_path = Path(project_root) / ".dgov" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, dict] = {}
    if config_path.is_file():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            logger.warning("Malformed TOML in %s: %s", config_path, exc)
            data = {}

    dgov_section = dict(data.get("dgov", {}))
    dgov_section[key] = value

    # Rebuild file: preserve other top-level sections, rewrite [dgov]
    lines: list[str] = []
    for section_name, section_data in data.items():
        if section_name == "dgov":
            continue
        lines.append(f"[{section_name}]")
        for k, v in section_data.items():
            lines.append(f'{k} = "{v}"')
        lines.append("")

    lines.append("[dgov]")
    for k, v in dgov_section.items():
        lines.append(f'{k} = "{v}"')
    lines.append("")

    config_path.write_text("\n".join(lines), encoding="utf-8")


def get_default_agent(registry: dict[str, AgentDef] | None = None) -> str:
    """Return the default agent to use when none is specified.

    Priority:
    1. User config: [dgov] default_agent in ~/.dgov/config.toml
    2. First installed from: claude → codex → gemini
    3. First installed agent in registry
    4. "claude" (ultimate fallback)
    """
    config = _load_dgov_config()
    configured = config.get("default_agent")
    if configured:
        return configured

    reg = registry or AGENT_REGISTRY
    installed = detect_installed_agents(reg)

    for agent_id in _DEFAULT_AGENT_CHAIN:
        if agent_id in installed:
            return agent_id

    if installed:
        return installed[0]

    return "claude"


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
