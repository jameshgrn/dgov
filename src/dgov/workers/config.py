"""Worker-facing config and payload translation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from dgov.tool_policy import ToolPolicy, parse_tool_policy


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
    llm_base_url: str = "https://api.fireworks.ai/inference/v1"
    llm_api_key_env: str = "FIREWORKS_API_KEY"
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
    proj = raw.get("project", {})
    flat: dict[str, Any] = dict(proj)
    flat["conventions"] = raw.get("conventions", {})
    flat["tool_policy"] = raw.get("tool_policy", {})
    return atomic_config_to_payload(atomic_config_from_payload(flat))
