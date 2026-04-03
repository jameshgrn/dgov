"""Unified dgov configuration loader."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, Any] = {
    "providers": {
        "plan_generation": {
            "transport": "claude-cli",
            "model": "claude-sonnet-4-6",
            "auth": "oauth",
            "timeout_s": 120,
        },
        "review": {
            "transport": "openrouter",
            "model": "qwen/qwen3.5-122b",
            "auth": "api",
        },
    },
    "defaults": {
        "agent": "qwen-35b",
        "timeout_s": 300,
        "max_retries": 2,
        "merge_strategy": "squash",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(project_root: str = ".") -> dict[str, Any]:
    """Load and merge config: defaults <- user <- project.

    Sources (later overrides earlier):
    1. Built-in defaults (_DEFAULTS)
    2. ~/.dgov/config.toml (user-level)
    3. <project_root>/.dgov/config.toml (project-level)
    """
    config = dict(_DEFAULTS)

    # User-level
    user_path = Path.home() / ".dgov" / "config.toml"
    user_config = _read_toml(user_path)
    if user_config:
        config = _deep_merge(config, user_config)

    # Project-level
    project_path = Path(project_root) / ".dgov" / "config.toml"
    project_config = _read_toml(project_path)
    if project_config:
        config = _deep_merge(config, project_config)

    return config


def get_provider_config(provider_name: str, project_root: str = ".") -> dict[str, Any]:
    """Get config for a specific provider with defaults applied."""
    config = load_config(project_root)
    providers = config.get("providers", {})
    defaults = _DEFAULTS.get("providers", {}).get(provider_name, {})
    return _deep_merge(defaults, providers.get(provider_name, {}))


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file, return empty dict on any error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}


def _coerce_value(value: str) -> str | int | float | bool:
    """Auto-coerce string values to appropriate types."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def write_config(
    key: str,
    value: str,
    scope: str = "user",
    project_root: str = ".",
) -> Path:
    """Set a dotted key in the config file.

    Args:
        key: Dotted key path like "providers.plan_generation.model"
        value: Value to set (strings stay strings, ints/bools auto-coerced)
        scope: "user" for ~/.dgov/config.toml, "project" for .dgov/config.toml
        project_root: Project root (used when scope="project")

    Returns:
        Path to the written config file.
    """
    import tomli_w

    if scope == "user":
        path = Path.home() / ".dgov" / "config.toml"
    elif scope == "project":
        path = Path(project_root) / ".dgov" / "config.toml"
    else:
        msg = f"Unknown scope: {scope!r} (expected 'user' or 'project')"
        raise ValueError(msg)

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_toml(path)

    # Parse dotted key into nested dict
    parts = key.split(".")
    target = existing
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = _coerce_value(value)

    with open(path, "wb") as f:
        tomli_w.dump(existing, f)

    return path
