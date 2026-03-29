# Agent registry and launch commands
"""Agent registry and launch command builder.

Built-in agents: claude, codex, gemini, opencode, cline, qwen, amp,
pi, cursor, copilot, crush.
Pi-routed variants: pi-claude, pi-codex, pi-gemini, pi-openrouter.
Users add custom agents via TOML config files.
"""

from __future__ import annotations

import logging
import random
import shlex
import shutil
import string
import time
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class DoneStrategyType(StrEnum):
    SIGNAL = "signal"
    EXIT = "exit"
    COMMIT = "commit"
    STABLE = "stable"
    API = "api"


@dataclass(frozen=True)
class DoneStrategy:
    """How to detect when an agent has finished its task.

    Strategy types:
    - "signal": Wait for done-signal file (default, current behavior).
    - "exit": Agent process exits — rely on done file + pane liveness, skip commit check.
    - "commit": Wait for new commits on branch — skip output stabilization.
    - "stable": Wait for output to stabilize for stable_seconds.
    - "api": Agent calls dgov worker complete/fail. Only checks signal files + liveness.
    """

    type: DoneStrategyType  # "signal" | "exit" | "commit" | "stable" | "api"
    stable_seconds: int = 15  # only used when type="stable"


class TransportType(StrEnum):
    POSITIONAL = "positional"
    OPTION = "option"
    SEND_KEYS = "send-keys"
    STDIN = "stdin"


@dataclass(frozen=True)
class PromptTransport:
    type: TransportType  # positional | option | send-keys | stdin
    option: str | None = None
    no_prompt_command: str | None = None
    pre_prompt: tuple[str, ...] = ()
    submit: tuple[str, ...] = ("Enter",)
    post_paste_delay_ms: int = 0
    ready_delay_ms: int = 0


@dataclass(frozen=True)
class HealthConfig:
    check: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 0
    escalate_to: str | None = None


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    short_label: str
    prompt_command: str
    transport: PromptTransport = field(
        default_factory=lambda: PromptTransport(type=TransportType.POSITIONAL)
    )
    health: HealthConfig = field(default_factory=HealthConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    permission_flags: dict[str, str] = field(default_factory=dict)
    interactive: bool = False
    default_flags: str = ""
    resume_template: str | None = None
    max_concurrent: int | None = None
    color: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    groups: tuple[str, ...] = ()
    done_strategy: DoneStrategy | None = None
    source: str = "built-in"


# Built-in agents: only public CLIs that dgov ships defaults for.
_BUILTIN_AGENTS: dict[str, AgentDef] = {
    "claude": AgentDef(
        id="claude",
        name="Claude Code",
        short_label="cc",
        prompt_command="claude",
        transport=PromptTransport(type=TransportType.POSITIONAL, ready_delay_ms=3000),
        default_flags="",
        permission_flags={
            "plan": "--permission-mode plan",
            "acceptEdits": "--permission-mode acceptEdits",
            "bypassPermissions": "--dangerously-skip-permissions",
        },
        resume_template="claude --continue{permissions}",
        color=39,
        interactive=True,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "codex": AgentDef(
        id="codex",
        name="Codex",
        short_label="cx",
        prompt_command="codex",
        transport=PromptTransport(type=TransportType.POSITIONAL),
        permission_flags={
            "acceptEdits": "--full-auto",
            "bypassPermissions": "--dangerously-bypass-approvals-and-sandbox",
        },
        interactive=True,
        color=214,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "gemini": AgentDef(
        id="gemini",
        name="Gemini CLI",
        short_label="gm",
        prompt_command="gemini",
        transport=PromptTransport(
            type=TransportType.OPTION, option="--prompt", ready_delay_ms=8000
        ),
        permission_flags={
            "plan": "--approval-mode plan",
            "acceptEdits": "--approval-mode auto_edit",
            "bypassPermissions": "--approval-mode yolo",
        },
        resume_template="gemini --resume latest{permissions}",
        color=135,
        interactive=True,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "opencode": AgentDef(
        id="opencode",
        name="OpenCode",
        short_label="oc",
        prompt_command="opencode",
        transport=PromptTransport(type=TransportType.OPTION, option="--prompt"),
        color=82,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "cline": AgentDef(
        id="cline",
        name="Cline CLI",
        short_label="cl",
        prompt_command="cline",
        transport=PromptTransport(
            type=TransportType.SEND_KEYS, post_paste_delay_ms=120, ready_delay_ms=2500
        ),
        permission_flags={
            "plan": "--plan",
            "acceptEdits": "--act",
            "bypassPermissions": "--act --yolo",
        },
        color=196,
        done_strategy=DoneStrategy(type=DoneStrategyType.STABLE, stable_seconds=30),
    ),
    "qwen": AgentDef(
        id="qwen",
        name="Qwen CLI",
        short_label="qn",
        prompt_command="qwen",
        transport=PromptTransport(type=TransportType.OPTION, option="-i"),
        permission_flags={
            "plan": "--approval-mode plan",
            "acceptEdits": "--approval-mode auto-edit",
            "bypassPermissions": "--approval-mode yolo",
        },
        resume_template="qwen --continue{permissions}",
        color=99,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "amp": AgentDef(
        id="amp",
        name="Amp CLI",
        short_label="ap",
        prompt_command="amp",
        transport=PromptTransport(type=TransportType.STDIN),
        permission_flags={
            "bypassPermissions": "--dangerously-allow-all",
        },
        color=208,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "pi": AgentDef(
        id="pi",
        name="Qwen 35B (River)",
        short_label="qw",
        prompt_command="pi",
        transport=PromptTransport(type=TransportType.STDIN),
        default_flags="-p",  # non-interactive: process prompt and exit
        permission_flags={
            "plan": "--tools read,grep,find,ls",
            "acceptEdits": "--tools read,grep,find,ls,edit",
            "bypassPermissions": "--tools read,grep,find,ls,edit,bash,write",
        },
        resume_template="pi --continue{permissions}",
        color=34,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "cursor": AgentDef(
        id="cursor",
        name="Cursor CLI",
        short_label="cr",
        prompt_command="cursor-agent",
        transport=PromptTransport(type=TransportType.POSITIONAL, ready_delay_ms=5000),
        permission_flags={
            "bypassPermissions": "--yolo",
        },
        color=45,
        interactive=True,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "copilot": AgentDef(
        id="copilot",
        name="Copilot CLI",
        short_label="co",
        prompt_command="copilot",
        transport=PromptTransport(type=TransportType.OPTION, option="-i"),
        permission_flags={
            "acceptEdits": "--allow-tool write",
            "bypassPermissions": "--allow-all",
        },
        resume_template="copilot --continue{permissions}",
        color=231,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "crush": AgentDef(
        id="crush",
        name="Crush CLI",
        short_label="cs",
        prompt_command="crush run",
        transport=PromptTransport(
            type=TransportType.SEND_KEYS,
            no_prompt_command="crush",
            pre_prompt=("Escape", "Tab"),
            submit=("Enter",),
            post_paste_delay_ms=200,
            ready_delay_ms=1200,
        ),
        permission_flags={
            "bypassPermissions": "--yolo",
        },
        color=219,
        done_strategy=DoneStrategy(type=DoneStrategyType.STABLE, stable_seconds=30),
    ),
    "pi-claude": AgentDef(
        id="pi-claude",
        name="pi → Claude",
        short_label="pc",
        prompt_command="pi",
        transport=PromptTransport(type=TransportType.POSITIONAL),
        default_flags="-p --provider anthropic --model claude-sonnet-4-20250514",
        color=39,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "pi-codex": AgentDef(
        id="pi-codex",
        name="pi → OpenAI",
        short_label="po",
        prompt_command="pi",
        transport=PromptTransport(type=TransportType.POSITIONAL),
        default_flags="-p --provider openai --model o3",
        color=214,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "pi-gemini": AgentDef(
        id="pi-gemini",
        name="pi → Gemini",
        short_label="pg",
        prompt_command="pi",
        transport=PromptTransport(type=TransportType.POSITIONAL),
        default_flags="-p --provider google --model gemini-2.5-pro",
        color=135,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
    "pi-openrouter": AgentDef(
        id="pi-openrouter",
        name="pi → OpenRouter",
        short_label="pr",
        prompt_command="pi",
        transport=PromptTransport(type=TransportType.POSITIONAL),
        default_flags="-p --provider openrouter",
        color=208,
        done_strategy=DoneStrategy(type=DoneStrategyType.API),
    ),
}


def _safe_mtime(path: Path) -> float:
    """Return mtime for a path, or 0 if it does not exist or is inaccessible."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# Module-level convenience alias (populated on first load_registry call or from built-ins).
AGENT_REGISTRY: dict[str, AgentDef] = dict(_BUILTIN_AGENTS)

# Cache for load_registry results, keyed by project root and config mtimes.
_registry_cache: dict[str, object] = {}


def _done_strategy_from_toml(table: dict) -> DoneStrategy | None:
    """Parse an optional [agents.X.done] section into a DoneStrategy."""
    done_section = table.pop("done", None)
    if not done_section:
        return None
    return DoneStrategy(
        type=DoneStrategyType(done_section.get("type", "signal")),
        stable_seconds=done_section.get("stable_seconds", 15),
    )


def _agent_def_from_toml(agent_id: str, table: dict, source: str) -> AgentDef:
    """Build an AgentDef from a TOML [agents.X] table."""
    permissions = table.pop("permissions", {})
    resume_section = table.pop("resume", {})
    env_section = table.pop("env", {})
    done_strategy = _done_strategy_from_toml(table)
    # Default to "api" when no [done] section — agent reports completion via dgov.
    if done_strategy is None:
        done_strategy = DoneStrategy(type=DoneStrategyType.API)

    # Parse transport section (new nested style) or flat fields (backward compat)
    transport_section = table.pop("transport", {})
    # Handle both: [transport] table (dict) and transport = "string" (str)
    if isinstance(transport_section, str):
        # Old format: transport = "positional"
        transport = PromptTransport(
            type=TransportType(transport_section),
            option=table.get("prompt_option"),
            no_prompt_command=table.get("no_prompt_command"),
            pre_prompt=tuple(table.get("send_keys_pre_prompt", ())),
            submit=tuple(table.get("send_keys_submit", ("Enter",))),
            post_paste_delay_ms=table.get("send_keys_post_paste_delay_ms", 0),
            ready_delay_ms=table.get("send_keys_ready_delay_ms", 0),
        )
    else:
        # New format: [transport] table
        transport = PromptTransport(
            type=TransportType(
                transport_section.get("type", table.get("transport", "positional"))
            ),
            option=transport_section.get("option", table.get("prompt_option")),
            no_prompt_command=transport_section.get(
                "no_prompt_command", table.get("no_prompt_command")
            ),
            pre_prompt=tuple(
                transport_section.get("pre_prompt", table.get("send_keys_pre_prompt", ()))
            ),
            submit=tuple(
                transport_section.get("submit", table.get("send_keys_submit", ("Enter",)))
            ),
            post_paste_delay_ms=transport_section.get(
                "post_paste_delay_ms", table.get("send_keys_post_paste_delay_ms", 0)
            ),
            ready_delay_ms=transport_section.get(
                "ready_delay_ms", table.get("send_keys_ready_delay_ms", 0)
            ),
        )

    # Parse health section (new nested style) or flat fields (backward compat)
    health_section = table.pop("health", {})
    if isinstance(health_section, dict):
        health = (
            HealthConfig(
                check=health_section.get("check", table.get("health_check")),
                fix=health_section.get("fix", table.get("health_fix")),
            )
            if (health_section or table.get("health_check") or table.get("health_fix"))
            else HealthConfig()
        )
    else:
        # Invalid format, fall back to flat fields or default
        health = HealthConfig(
            check=table.get("health_check"),
            fix=table.get("health_fix"),
        )

    # Parse retry section (new nested style) or flat fields (backward compat)
    retry_section = table.pop("retry", {})
    if isinstance(retry_section, dict):
        retry = (
            RetryConfig(
                max_retries=retry_section.get("max_retries", table.get("max_retries", 0)),
                escalate_to=retry_section.get("escalate_to", table.get("retry_escalate_to")),
            )
            if (retry_section or table.get("max_retries") or table.get("retry_escalate_to"))
            else RetryConfig()
        )
    else:
        # Invalid format, fall back to flat fields or default
        retry = RetryConfig(
            max_retries=table.get("max_retries", 0),
            escalate_to=table.get("retry_escalate_to"),
        )

    return AgentDef(
        id=agent_id,
        name=table.get("name", agent_id),
        short_label=table.get("short_label", agent_id[:2]),
        prompt_command=table["command"],
        transport=transport,
        health=health,
        retry=retry,
        permission_flags=dict(permissions),
        interactive=table.get("interactive", False),
        default_flags=table.get("default_flags", ""),
        resume_template=resume_section.get("template") or table.get("resume_template"),
        max_concurrent=table.get("max_concurrent"),
        color=table.get("color"),
        env=dict(env_section),
        groups=tuple(table.get("groups", ())),
        done_strategy=done_strategy,
        source=source,
    )


# -- Field resolver helpers for _merge_agent_def dispatch table -----------
#
# Each resolver takes the same positional args (base, overrides, source,
# permissions, resume_section, env_section, done_strategy, transport,
# health, retry) so the dispatch loop can call them uniformly.


def _resolve_source(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> str:
    return source


def _resolve_permission_flags(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> dict[str, str]:
    return dict(permissions) if permissions is not None else base.permission_flags


def _resolve_resume_template(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> str | None:
    if resume_section and "template" in resume_section:
        return resume_section["template"]
    if "resume_template" in overrides:
        return overrides["resume_template"]
    return base.resume_template


def _resolve_env(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> dict[str, str]:
    if env_section is not None:
        merged_env = dict(base.env)
        merged_env.update(env_section)
        return merged_env
    return base.env


def _resolve_done_strategy(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> DoneStrategy | None:
    return done_strategy if done_strategy is not None else base.done_strategy


def _resolve_transport(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> PromptTransport:
    return transport


def _resolve_health(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> HealthConfig:
    return health


def _resolve_retry(
    base: AgentDef,
    overrides: dict,
    source: str,
    permissions: dict | None,
    resume_section: dict | None,
    env_section: dict | None,
    done_strategy: DoneStrategy | None,
    transport: PromptTransport,
    health: HealthConfig,
    retry: RetryConfig,
) -> RetryConfig:
    return retry


_FIELD_RESOLVERS: dict[str, callable] = {
    "source": _resolve_source,
    "permission_flags": _resolve_permission_flags,
    "resume_template": _resolve_resume_template,
    "env": _resolve_env,
    "done_strategy": _resolve_done_strategy,
    "transport": _resolve_transport,
    "health": _resolve_health,
    "retry": _resolve_retry,
}

_TOML_KEY_MAP: dict[str, str] = {
    "prompt_command": "command",
}


def _merge_agent_def(base: AgentDef, overrides: dict, source: str) -> AgentDef:
    """Merge TOML overrides onto an existing AgentDef, producing a new one."""
    permissions = overrides.pop("permissions", None)
    resume_section = overrides.pop("resume", None)
    env_section = overrides.pop("env", None)
    done_strategy = _done_strategy_from_toml(overrides)

    # Parse nested sections for sub-objects
    transport_section = overrides.pop("transport", None)
    health_section = overrides.pop("health", None)
    retry_section = overrides.pop("retry", None)

    # Build transport: start with base, apply overrides
    if transport_section is not None:
        if isinstance(transport_section, str):
            # Old format: transport = "positional" (string, not dict)
            transport = PromptTransport(
                type=TransportType(transport_section),
                option=overrides.get("prompt_option", base.transport.option),
                no_prompt_command=overrides.get(
                    "no_prompt_command", base.transport.no_prompt_command
                ),
                pre_prompt=tuple(overrides.get("send_keys_pre_prompt", base.transport.pre_prompt)),
                submit=tuple(overrides.get("send_keys_submit", base.transport.submit)),
                post_paste_delay_ms=overrides.get(
                    "send_keys_post_paste_delay_ms", base.transport.post_paste_delay_ms
                ),
                ready_delay_ms=overrides.get(
                    "send_keys_ready_delay_ms", base.transport.ready_delay_ms
                ),
            )
        else:
            # New format: [transport] table
            transport = PromptTransport(
                type=TransportType(transport_section.get("type", base.transport.type)),
                option=transport_section.get("option", base.transport.option),
                no_prompt_command=transport_section.get(
                    "no_prompt_command", base.transport.no_prompt_command
                ),
                pre_prompt=tuple(transport_section.get("pre_prompt", base.transport.pre_prompt)),
                submit=tuple(transport_section.get("submit", base.transport.submit)),
                post_paste_delay_ms=transport_section.get(
                    "post_paste_delay_ms", base.transport.post_paste_delay_ms
                ),
                ready_delay_ms=transport_section.get(
                    "ready_delay_ms", base.transport.ready_delay_ms
                ),
            )
    elif any(
        k in overrides
        for k in (
            "transport",
            "prompt_option",
            "no_prompt_command",
            "send_keys_pre_prompt",
            "send_keys_submit",
            "send_keys_post_paste_delay_ms",
            "send_keys_ready_delay_ms",
        )
    ):
        # Backward compat: flat field overrides
        transport = PromptTransport(
            type=TransportType(overrides.get("transport", base.transport.type)),
            option=overrides.get("prompt_option", base.transport.option),
            no_prompt_command=overrides.get("no_prompt_command", base.transport.no_prompt_command),
            pre_prompt=tuple(overrides.get("send_keys_pre_prompt", base.transport.pre_prompt)),
            submit=tuple(overrides.get("send_keys_submit", base.transport.submit)),
            post_paste_delay_ms=overrides.get(
                "send_keys_post_paste_delay_ms", base.transport.post_paste_delay_ms
            ),
            ready_delay_ms=overrides.get(
                "send_keys_ready_delay_ms", base.transport.ready_delay_ms
            ),
        )
    else:
        transport = base.transport

    # Build health: start with base, apply overrides
    if health_section is not None:
        health = HealthConfig(
            check=health_section.get("check", base.health.check),
            fix=health_section.get("fix", base.health.fix),
        )
    elif any(k in overrides for k in ("health_check", "health_fix")):
        # Backward compat: flat field overrides
        health = HealthConfig(
            check=overrides.get("health_check", base.health.check),
            fix=overrides.get("health_fix", base.health.fix),
        )
    else:
        health = base.health

    # Build retry: start with base, apply overrides
    if retry_section is not None:
        retry = RetryConfig(
            max_retries=retry_section.get("max_retries", base.retry.max_retries),
            escalate_to=retry_section.get("escalate_to", base.retry.escalate_to),
        )
    elif any(k in overrides for k in ("max_retries", "retry_escalate_to")):
        # Backward compat: flat field overrides
        retry = RetryConfig(
            max_retries=overrides.get("max_retries", base.retry.max_retries),
            escalate_to=overrides.get("retry_escalate_to", base.retry.escalate_to),
        )
    else:
        retry = base.retry

    kwargs: dict = {}
    for f in AgentDef.__dataclass_fields__:
        resolver = _FIELD_RESOLVERS.get(f)
        if resolver:
            kwargs[f] = resolver(
                base,
                overrides,
                source,
                permissions,
                resume_section,
                env_section,
                done_strategy,
                transport,
                health,
                retry,
            )
        else:
            # Map TOML key names to dataclass field names
            toml_key = _TOML_KEY_MAP.get(f, f)
            if toml_key in overrides:
                kwargs[f] = overrides[toml_key]
            elif f in overrides:
                kwargs[f] = overrides[f]
            else:
                kwargs[f] = getattr(base, f)

    # Handle tuple fields
    for tf in ("groups",):
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
    # Normalize project_root to a string for caching purposes
    normalized_root = str(project_root) if project_root is not None else None

    # Compute current mtimes for user and project config files
    user_config = Path.home() / ".dgov" / "agents.toml"
    user_mtime = _safe_mtime(user_config)
    project_config = (Path(normalized_root) / ".dgov" / "agents.toml") if normalized_root else None
    project_mtime = _safe_mtime(project_config) if project_config is not None else 0.0

    cached_project_root = _registry_cache.get("project_root")
    cached_user_mtime = _registry_cache.get("user_mtime")
    cached_project_mtime = _registry_cache.get("project_mtime")

    if (
        cached_project_root == normalized_root
        and cached_user_mtime == user_mtime
        and cached_project_mtime == project_mtime
        and "result" in _registry_cache
    ):
        return _registry_cache["result"]  # type: ignore[return-value]

    registry = dict(_BUILTIN_AGENTS)

    # User global: ~/.dgov/agents.toml
    for agent_id, table in _load_toml_file(user_config).items():
        table = dict(table)  # shallow copy so pops don't mutate cache
        if agent_id in registry:
            registry[agent_id] = _merge_agent_def(registry[agent_id], table, "user")
        else:
            registry[agent_id] = _agent_def_from_toml(agent_id, table, "user")

    # Project-local: <project_root>/.dgov/agents.toml
    if project_config is not None:
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
            # Also filter nested unsafe keys from [transport] and [health] sections
            if "transport" in table:
                table["transport"].pop("no_prompt_command", None)
            if "health" in table:
                table["health"].pop("check", None)
                table["health"].pop("fix", None)
            if "env" in table:
                table.pop("env")
            if agent_id in registry:
                registry[agent_id] = _merge_agent_def(registry[agent_id], table, "project")
            # Project config cannot define NEW agents, only override existing ones

    _registry_cache["result"] = registry
    _registry_cache["project_root"] = normalized_root
    _registry_cache["user_mtime"] = user_mtime
    _registry_cache["project_mtime"] = project_mtime

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
_DEFAULT_AGENT_CHAIN = ("pi", "claude", "codex", "gemini")

# Logical Qwen worker routes (preferred over installed-agent chain)
_QWEN_WORKER_ROUTES = ("qwen-9b", "qwen-35b", "qwen-122b", "qwen-397b", "qwen-4b")


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
    2. Logical Qwen worker routes (if available):
       qwen-9b → qwen-35b → qwen-122b → qwen-397b → qwen-4b
    3. First installed from: claude → codex → gemini
    4. First installed agent in registry
    5. "claude" (ultimate fallback)
    """
    config = _load_dgov_config()
    configured = config.get("default_agent")
    if configured:
        return configured

    # Try logical Qwen worker routes first (uses router.resolve_agent internally)
    try:
        from dgov.router import resolve_agent

        session_root = str(Path.home() / ".dgov/sessions")
        project_root = "."
        for route in _QWEN_WORKER_ROUTES:
            try:
                physical, _ = resolve_agent(route, session_root, project_root)
                # If we got here without exception, the backend resolved successfully
                if physical != route or any(backend in route for backend in ["river", "qwen35"]):
                    # Physical backend was found and routed successfully
                    return route
            except RuntimeError:
                continue
    except ImportError:
        pass  # Router not available, skip logical routes

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
    force_headless: bool = False,
) -> str:
    """Build the shell command to launch an agent with an optional prompt.

    For positional and option transports, writes prompt to a temp file
    and builds a shell snippet that reads+deletes it (avoids escaping issues).

    When *force_headless* is True, the prompt is always embedded in the
    command even if the agent is marked interactive (used for workers).

    Returns the full shell command string. For send-keys transport agents,
    returns just the base command (prompt delivered separately via tmux buffer).
    """
    reg = registry or AGENT_REGISTRY
    agent = reg[agent_id]
    flags = _perm_flags(agent, permission_mode)
    base = agent.prompt_command
    # Codex headless workers need "exec" subcommand for non-interactive mode
    if force_headless and agent.prompt_command == "codex":
        base = "codex exec"
    if agent.default_flags:
        df = agent.default_flags
        # Avoid "codex exec exec ..." when force_headless already added exec
        if force_headless and agent.prompt_command == "codex" and df.startswith("exec "):
            df = df[5:]  # strip leading "exec "
        base = f"{base} {df}"
    # Skip permission flags if extra_flags already contains any of the same --flag names
    if flags and extra_flags:
        flag_names = {t for t in flags.split() if t.startswith("-")}
        extra_flag_names = {t for t in extra_flags.split() if t.startswith("-")}
        if flag_names & extra_flag_names:
            flags = ""
    if flags:
        base = f"{base} {flags}"
    if extra_flags:
        base = f"{base} {extra_flags}"

    if not prompt:
        return agent.transport.no_prompt_command or base

    if agent.interactive and prompt and not force_headless:
        return agent.transport.no_prompt_command or base

    if agent.transport.type == TransportType.SEND_KEYS:
        return agent.transport.no_prompt_command or base

    prompt_file = _write_prompt_file(project_root, slug, prompt)
    snippet = _prompt_read_and_delete_snippet(prompt_file)

    # Inject worker instructions as system prompt for pi workers
    instructions_path = Path(project_root) / ".dgov" / "DGOV_SYSTEM_PROMPT.md"
    if not instructions_path.exists():
        instructions_path = Path(project_root) / ".dgov" / "DGOV_WORKER_INSTRUCTIONS.md"
    if agent.prompt_command == "pi" and instructions_path.exists():
        instr = shlex.quote(str(instructions_path))
        base = f"{base} --append-system-prompt {instr}"

    if agent.transport.type == TransportType.STDIN:
        return f"{snippet}; printf '%s\\n' \"$DGOV_PROMPT_CONTENT\" | {base}"

    if agent.transport.type == TransportType.OPTION and agent.transport.option:
        return f'{snippet}; {base} {agent.transport.option} "$DGOV_PROMPT_CONTENT"'

    # positional
    return f'{snippet}; {base} "$DGOV_PROMPT_CONTENT"'


# ---------------------------------------------------------------------------
# Agent protocol — formal contract for dgov workers
# ---------------------------------------------------------------------------

_VALID_COMPLETIONS = frozenset({"api", "exit", "signal", "commit", "stable"})


@dataclass(frozen=True)
class AgentProtocol:
    """Formal contract for dgov worker agents.

    An agent MUST:
    - Accept a task prompt (via its transport mechanism)
    - Work within the assigned git worktree (cwd)
    - Commit changes to the worktree branch
    - Signal completion via one of:
      a) calling ``dgov worker complete`` (preferred)
      b) exiting with code 0 (auto-detected)
      c) producing commits and becoming idle (fallback)

    An agent SHOULD:
    - Only modify files declared in its file claims
    - Not modify protected files (CLAUDE.md, CODEBASE.md)
    - Complete within the timeout period

    An agent MUST NOT:
    - Write to files outside the worktree
    - Push to remote repositories
    - Modify the main branch directly
    """

    transport: str  # how prompt is delivered
    completion: str  # how done is signaled
    isolation: str = "worktree"
    supports_tools: bool = True
    headless: bool = False


def validate_agent_protocol(agent_id: str, registry: dict[str, AgentDef]) -> list[str]:
    """Return list of protocol violations for an agent definition.

    Checks that the agent definition conforms to the formal protocol.
    Empty list = fully compliant.
    """
    violations: list[str] = []
    defn = registry.get(agent_id)
    if defn is None:
        return [f"Agent '{agent_id}' not found in registry"]

    try:
        TransportType(defn.transport.type)
    except ValueError:
        violations.append(
            f"Invalid transport '{defn.transport.type}' "
            f"(must be one of {sorted(TransportType.__members__.values())})"
        )

    if not defn.prompt_command:
        violations.append("No prompt_command defined")

    done_type = defn.done_strategy.type if defn.done_strategy else "api"
    if done_type not in _VALID_COMPLETIONS:
        violations.append(
            f"Invalid completion strategy '{done_type}' "
            f"(must be one of {sorted(_VALID_COMPLETIONS)})"
        )

    if defn.transport.type == TransportType.SEND_KEYS and not defn.transport.submit:
        violations.append("send-keys transport requires submit sequence")

    if defn.transport.type == TransportType.OPTION and not defn.transport.option:
        violations.append("option transport requires option flag")

    return violations


def check_all_agents(registry: dict[str, AgentDef]) -> dict[str, list[str]]:
    """Validate all agents in the registry. Returns {agent_id: [violations]}."""
    results = {}
    for agent_id in registry:
        violations = validate_agent_protocol(agent_id, registry)
        if violations:
            results[agent_id] = violations
    return results


def load_groups(project_root: str | None = None) -> dict[str, dict]:
    """Load agent group definitions from TOML config files.

    Returns {group_id: {max_concurrent: int, ...}}.
    """
    groups: dict[str, dict] = {}

    # User global: ~/.dgov/agents.toml
    user_config = Path.home() / ".dgov" / "agents.toml"
    if user_config.is_file():
        try:
            with open(user_config, "rb") as f:
                data = tomllib.load(f)
                groups.update(data.get("groups", {}))
        except (tomllib.TOMLDecodeError, OSError):
            pass

    # Project-local: <project_root>/.dgov/agents.toml
    if project_root:
        project_config = Path(project_root) / ".dgov" / "agents.toml"
        if project_config.is_file():
            try:
                with open(project_config, "rb") as f:
                    data = tomllib.load(f)
                    groups.update(data.get("groups", {}))
            except (tomllib.TOMLDecodeError, OSError):
                pass

    return groups


def _resolve_aliases(
    routing_dict: dict[str, list[str]],
    raw_routing: dict[str, dict],
) -> dict[str, list[str]]:
    """Resolve alias_for entries to their target's backends.

    For each entry with alias_for but not backends, looks up the target
    in routing_dict and copies its backend list. Only resolves one level
    deep (no recursion). Logs a warning if the target doesn't exist.
    """
    log = logging.getLogger(__name__)
    for name, table in raw_routing.items():
        if isinstance(table, dict) and "alias_for" in table and "backends" not in table:
            target = table["alias_for"]
            if target in routing_dict:
                routing_dict[name] = list(routing_dict[target])
            else:
                log.warning(
                    "Routing alias '%s' -> '%s' unresolved (target not found)",
                    name,
                    target,
                )
    return routing_dict


def load_routing_tables(
    project_root: str | None = None,
) -> dict[str, list[str]]:
    """Load routing tables from TOML config files.

    Returns {logical_name: [backend1, backend2, ...]}.

    Priority order (project-local takes precedence over user-global):
    1. Project-local: <project_root>/.dgov/agents.toml [routing.*]
    2. User global: ~/.dgov/agents.toml [routing.*]

    Project-local routes override user-global routes for the same logical name.
    If neither exists, returns empty dict.
    """
    from pathlib import Path

    result: dict[str, list[str]] = {}
    raw_routing: dict[str, dict] = {}

    # Load user-global first (base configuration)
    # Project-local will override for routes defined locally
    user_config = Path.home() / ".dgov" / "agents.toml"
    if user_config.is_file():
        try:
            with open(user_config, "rb") as f:
                data = tomllib.load(f)
            routing = data.get("routing", {})
            for name, table in routing.items():
                if isinstance(table, dict) and "backends" in table:
                    result[name] = list(table["backends"])
                elif isinstance(table, dict):
                    raw_routing[name] = table
        except (tomllib.TOMLDecodeError, OSError):
            pass

    # Project-local overrides user-global for same logical names
    if project_root:
        project_config = Path(project_root) / ".dgov" / "agents.toml"
        if project_config.is_file():
            try:
                with open(project_config, "rb") as f:
                    data = tomllib.load(f)
                routing = data.get("routing", {})
                for name, table in routing.items():
                    if isinstance(table, dict) and "backends" in table:
                        result[name] = list(table["backends"])
                    elif isinstance(table, dict):
                        raw_routing[name] = table
            except (tomllib.TOMLDecodeError, OSError):
                pass

    # Resolve alias_for entries (one level deep, no recursion)
    _resolve_aliases(result, raw_routing)

    return result
