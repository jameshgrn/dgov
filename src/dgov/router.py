"""Agent router: resolve logical model names to available physical backends.

Maps logical names (qwen-35b, qwen-9b, etc.) to ordered pools of physical
backends (river-35b, qwen35-35b, etc.). Checks health and concurrency to
pick the first available backend.

Includes circuit breaker for backends with recent failures.
"""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import time
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_routing_cache: dict[str, object] = {}


def _load_routing_tables() -> dict[str, list[str]]:
    """Load [routing.*] tables from ~/.dgov/agents.toml.

    Returns {logical_name: [backend1, backend2, ...]}.
    """
    config_path = Path.home() / ".dgov" / "agents.toml"

    mtime = 0.0
    if config_path.is_file():
        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            pass

    if _routing_cache.get("mtime") == mtime and "tables" in _routing_cache:
        return _routing_cache["tables"]  # type: ignore[return-value]

    result: dict[str, list[str]] = {}
    if mtime > 0:
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            routing = data.get("routing", {})
            for name, table in routing.items():
                if isinstance(table, dict) and "backends" in table:
                    result[name] = list(table["backends"])
        except (tomllib.TOMLDecodeError, OSError):
            pass

    _routing_cache["mtime"] = mtime
    _routing_cache["tables"] = result
    return result


def is_routable(name: str) -> bool:
    """Check if a name is a logical routing key."""
    return name in _load_routing_tables()


def available_names() -> list[str]:
    """Return all logical routing names."""
    return sorted(_load_routing_tables())


def physical_to_logical(physical_name: str) -> str:
    """Map a physical backend name back to its logical routing name.

    Returns the physical name unchanged if no mapping is found.
    """
    for logical, backends in _load_routing_tables().items():
        if physical_name in backends:
            return logical
    return physical_name


def record_backend_failure(session_root: str, backend_id: str) -> None:
    """Record a backend failure and prune old entries.

    Appends current timestamp to JSON file at session_root/.dgov/backend_failures.json.
    Prunes all entries older than 10 minutes on each write.
    Uses fcntl.flock(LOCK_EX) for concurrency safety.
    If file is unreadable/corrupt, silently resets to empty dict.
    """
    failures_file = Path(session_root) / ".dgov" / "backend_failures.json"
    failures_file.parent.mkdir(parents=True, exist_ok=True)

    now = time.time()

    try:
        with open(failures_file, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                data = json.load(f)
            except (json.JSONDecodeError, KeyError):
                data = {}
    except (OSError, IOError):
        data = {}

    if backend_id not in data:
        data[backend_id] = []
    data[backend_id].append(now)

    # Prune all backends' old entries
    for bid in list(data.keys()):
        data[bid] = [ts for ts in data[bid] if now - ts < 600]
        if not data[bid]:
            del data[bid]

    try:
        with open(failures_file, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f)
    except (OSError, IOError):
        pass


def _check_circuit_breaker(
    session_root: str, backend_id: str, threshold: int = 2, window_minutes: int = 10
) -> bool:
    """Check if circuit breaker is tripped for a backend.

    Returns True if backend has >= threshold failures within the last window_minutes.
    Returns False if file missing, unreadable, or backend not in file.
    Never raises — fails open (returns False on any error).
    """
    failures_file = Path(session_root) / ".dgov" / "backend_failures.json"

    try:
        with open(failures_file, "r") as f:
            data = json.load(f)
    except (OSError, IOError, json.JSONDecodeError):
        return False

    if backend_id not in data:
        return False

    now = time.time()
    cutoff = now - (window_minutes * 60)
    recent_failures = [ts for ts in data[backend_id] if ts > cutoff]

    return len(recent_failures) >= threshold


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
    from dgov.agents import load_groups, load_registry
    from dgov.backend import get_backend
    from dgov.persistence import all_panes
    from dgov.status import _count_active_agent_workers

    tables = _load_routing_tables()
    if name not in tables:
        return name, None

    backends = tables[name]
    registry = load_registry(project_root)
    groups = load_groups(project_root)

    # Optimization: fetch all active panes and tmux info once for group checks
    _TERMINAL_STATES = {
        "done",
        "failed",
        "superseded",
        "merged",
        "closed",
        "escalated",
        "timed_out",
    }
    panes = all_panes(session_root)
    all_tmux = get_backend().bulk_info()

    group_counts: dict[str, int] = {}
    for p in panes:
        agent_id = p.get("agent", "")
        if agent_id and p.get("state") not in _TERMINAL_STATES:
            pane_id = p.get("pane_id", "")
            if pane_id and pane_id in all_tmux:
                # Track group counts
                agent_def = registry.get(agent_id)
                if agent_def:
                    agent_groups = getattr(agent_def, "groups", ())
                    for g in agent_groups:
                        group_counts[g] = group_counts.get(g, 0) + 1

    tried: list[str] = []
    for backend_id in backends:
        agent_def = registry.get(backend_id)
        if agent_def is None:
            tried.append(f"{backend_id} (not registered)")
            continue

        # Circuit breaker check
        if _check_circuit_breaker(session_root, backend_id):
            tried.append(f"{backend_id} (circuit breaker tripped)")
            continue

        # Group Concurrency Check
        agent_groups = getattr(agent_def, "groups", ())
        if agent_groups:
            group_blocked = False
            for g in agent_groups:
                g_def = groups.get(g)
                if g_def and "max_concurrent" in g_def:
                    active = group_counts.get(g, 0)
                    limit = g_def["max_concurrent"]
                    if active >= limit:
                        tried.append(f"{backend_id} (group '{g}' full: {active}/{limit})")
                        group_blocked = True
                        break
            if group_blocked:
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

        # Individual Concurrency check
        max_concurrent = agent_def.max_concurrent
        if max_concurrent is not None:
            active = _count_active_agent_workers(session_root, backend_id)
            if active >= max_concurrent:
                tried.append(f"{backend_id} ({active}/{max_concurrent} busy)")
                continue

        logger.info("Routed %s -> %s", name, backend_id)
        return backend_id, name

    raise RuntimeError(
        f"No available backend for '{name}'. "
        f"Tried: {', '.join(tried)}. "
        f"All backends busy or unhealthy."
    )
