"""Worker-facing config and payload translation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from dgov.tool_policy import ToolPolicy, parse_tool_policy

DEFAULT_LLM_PROVIDER = ""
DEFAULT_LLM_BASE_URL = ""
DEFAULT_LLM_API_KEY_ENV = ""


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved OpenAI-compatible provider endpoint."""

    name: str
    base_url: str
    api_key_env: str
    default_agent: str = ""


@dataclass(frozen=True)
class AtomicConfig:
    """Worker-facing project config.

    Single source of truth for every field the worker subprocess consumes.
    ProjectConfig inherits from this and adds governor-only fields. The worker
    subprocess never imports ProjectConfig, preserving subprocess isolation.
    """

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    llm_provider: str = DEFAULT_LLM_PROVIDER
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key_env: str = DEFAULT_LLM_API_KEY_ENV
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    lint_fix_cmd: str = "python -m ruff check --fix --unsafe-fixes {file}"
    type_check_cmd: str | None = None
    worker_iteration_budget: int = 50
    worker_iteration_warn_at: int = 40
    worker_tree_max_lines: int = 80
    line_length: int = 99
    test_markers: tuple[str, ...] = ()
    # User-defined project conventions from [conventions] in project.toml.
    conventions: dict[str, str] = field(default_factory=dict)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)


def _coerce_markers(v: object) -> tuple[str, ...]:
    if isinstance(v, (list, tuple)):
        return tuple(str(item) for item in v)
    return ()


def _coerce_conventions(v: object) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    return {str(key): str(value) for key, value in v.items()}


def _coerce_tool_policy(v: object) -> ToolPolicy:
    return parse_tool_policy(v if isinstance(v, dict) else {})


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in raw:
        return {}
    value = raw[key]
    if not isinstance(value, dict):
        raise ValueError(f".dgov/project.toml [{key}] must be a table")
    return value


def _provider_name(proj: Mapping[str, Any], providers: Mapping[str, ProviderConfig]) -> str:
    if "provider" in proj:
        raw = proj["provider"]
        if not isinstance(raw, str):
            raise ValueError(".dgov/project.toml [project].provider must be a string")
        name = raw.strip()
        if not name:
            raise ValueError(".dgov/project.toml [project].provider must be a non-empty string")
        return name
    if len(providers) == 1:
        return next(iter(providers))
    return ""


def _string_field(
    data: Mapping[str, Any],
    key: str,
    *,
    context: str,
    required: bool = True,
) -> str:
    raw = data.get(key)
    if raw is None:
        value = ""
    elif isinstance(raw, str):
        value = raw.strip()
    else:
        raise ValueError(f".dgov/project.toml {context}.{key} must be a string")
    if required and not value:
        raise ValueError(f".dgov/project.toml {context}.{key} must be a non-empty string")
    return value


def _provider_from_table(name: str, data: Mapping[str, Any]) -> ProviderConfig:
    context = f"[providers.{name}]"
    return ProviderConfig(
        name=name,
        base_url=_string_field(data, "base_url", context=context),
        api_key_env=_string_field(data, "api_key_env", context=context),
        default_agent=_string_field(
            data,
            "default_agent",
            context=context,
            required=False,
        ),
    )


def provider_configs_from_project_toml(raw: Mapping[str, Any]) -> dict[str, ProviderConfig]:
    """Parse named provider definitions from raw project TOML."""
    providers_raw = _table(raw, "providers")
    providers: dict[str, ProviderConfig] = {}
    for name, data in providers_raw.items():
        provider_name = str(name).strip()
        if not provider_name:
            raise ValueError(".dgov/project.toml [providers] names must be non-empty")
        if not isinstance(data, dict):
            raise ValueError(f".dgov/project.toml [providers.{provider_name}] must be a table")
        providers[provider_name] = _provider_from_table(provider_name, data)
    return providers


def selected_provider_from_project_toml(raw: Mapping[str, Any]) -> ProviderConfig:
    """Resolve the active worker provider from project.toml data.

    The returned provider is flattened for worker subprocess payloads. A repo
    with a single provider may omit `[project].provider`; repos with multiple
    providers must select a default or set `provider` on every task.
    """
    proj = _table(raw, "project")
    providers = provider_configs_from_project_toml(raw)
    name = _provider_name(proj, providers)
    if not name:
        return ProviderConfig(name="", base_url="", api_key_env="")
    if name in providers:
        return providers[name]

    raise ValueError(
        f".dgov/project.toml selects provider {name!r}, but [providers.{name}] is not defined"
    )


# Per-field payload to AtomicConfig coercion. Fields omitted from this map
# are assigned directly (str/int/bool). One entry per non-trivial type.
_PAYLOAD_COERCERS: dict[str, Callable[[object], object]] = {
    "test_markers": _coerce_markers,
    "conventions": _coerce_conventions,
    "tool_policy": _coerce_tool_policy,
}

# Per-field AtomicConfig to JSON-serializable coercion. Must be idempotent
# with _PAYLOAD_COERCERS so payload to config to payload round-trips.
_PAYLOAD_SERIALIZERS: dict[str, Callable[[object], object]] = {
    "test_markers": lambda v: list(v) if v else [],
    "conventions": lambda v: dict(v) if v else {},
    "tool_policy": lambda v: v.as_jsonable() if isinstance(v, ToolPolicy) else {},
}


def atomic_config_from_payload(raw: Mapping[str, Any]) -> AtomicConfig:
    """Deserialize a worker payload into AtomicConfig."""
    values: dict[str, Any] = {}
    for f in fields(AtomicConfig):
        if f.name not in raw:
            continue
        coercer = _PAYLOAD_COERCERS.get(f.name)
        values[f.name] = coercer(raw[f.name]) if coercer else raw[f.name]
    return AtomicConfig(**values)


def atomic_config_to_payload(config: AtomicConfig) -> dict[str, object]:
    """Serialize AtomicConfig into a JSON-safe worker payload."""
    payload: dict[str, object] = {}
    for f in fields(AtomicConfig):
        value = getattr(config, f.name)
        serializer = _PAYLOAD_SERIALIZERS.get(f.name)
        payload[f.name] = serializer(value) if serializer else value
    return payload


def worker_payload_from_project_toml(raw: dict[str, Any]) -> dict[str, object]:
    """Normalize raw project.toml data into the worker payload shape."""
    proj = _table(raw, "project")
    provider = selected_provider_from_project_toml(raw)
    flat: dict[str, Any] = dict(proj)
    flat["llm_provider"] = provider.name
    flat["llm_base_url"] = provider.base_url
    flat["llm_api_key_env"] = provider.api_key_env
    flat["conventions"] = _table(raw, "conventions")
    flat["tool_policy"] = _table(raw, "tool_policy")
    return atomic_config_to_payload(atomic_config_from_payload(flat))
