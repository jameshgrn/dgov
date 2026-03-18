"""Agent router: resolve logical model names to available physical backends.

Maps logical names (qwen-35b, qwen-9b, etc.) to ordered pools of physical
backends (river-35b, qwen35-35b, etc.). Checks health and concurrency to
pick the first available backend.
"""

from __future__ import annotations

import logging
import subprocess
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_routing_cache: dict[str, object] = {}


def _load_routing_tables() -> dict[str, list[str]]:
    """Load [routing.*] tables from ~/.dgov/agents.toml.

    Returns {logical_name: [backend1, backend2, ...]}.
    """
    config_path = Path.home() / ".dgov" / "agents.toml"
    if not config_path.is_file():
        return {}

    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return {}

    if _routing_cache.get("mtime") == mtime and "tables" in _routing_cache:
        return _routing_cache["tables"]  # type: ignore[return-value]

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return {}

    routing = data.get("routing", {})
    result: dict[str, list[str]] = {}
    for name, table in routing.items():
        if isinstance(table, dict) and "backends" in table:
            result[name] = list(table["backends"])

    _routing_cache["mtime"] = mtime
    _routing_cache["tables"] = result
    return result


def is_routable(name: str) -> bool:
    """Check if a name is a logical routing key."""
    return name in _load_routing_tables()


def available_names() -> list[str]:
    """Return all logical routing names."""
    return sorted(_load_routing_tables())


def resolve_agent(
    name: str,
    session_root: str,
    project_root: str,
) -> tuple[str, str | None]:
    """Resolve a logical agent name to an available physical backend.

    Returns (physical_agent_id, logical_name | None).
    If name is not a routing key, returns (name, None) unchanged.
    Checks health and concurrency for each backend in order.
    """
    from dgov.agents import load_registry
    from dgov.status import _count_active_agent_workers

    tables = _load_routing_tables()
    if name not in tables:
        return name, None

    backends = tables[name]
    registry = load_registry(project_root)

    tried: list[str] = []
    for backend_id in backends:
        agent_def = registry.get(backend_id)
        if agent_def is None:
            tried.append(f"{backend_id} (not registered)")
            continue

        # Health check
        if agent_def.health_check:
            try:
                hc = subprocess.run(
                    agent_def.health_check,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if hc.returncode != 0:
                    tried.append(f"{backend_id} (unhealthy)")
                    continue
            except (subprocess.TimeoutExpired, OSError):
                tried.append(f"{backend_id} (health check timeout)")
                continue

        # Concurrency check
        if agent_def.max_concurrent is not None:
            active = _count_active_agent_workers(session_root, backend_id)
            if active >= agent_def.max_concurrent:
                tried.append(f"{backend_id} ({active}/{agent_def.max_concurrent} busy)")
                continue

        logger.info("Routed %s -> %s", name, backend_id)
        return backend_id, name

    raise RuntimeError(
        f"No available backend for \x27{name}\x27. "
        f"Tried: {', '.join(tried)}. "
        f"All backends busy or unhealthy."
    )
