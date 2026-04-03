"""Real agent router — resolves logical names to concrete backends.

Routes are defined in .dgov/agents.toml under [routing.*] sections.
Supports:
- Direct backend names (kimi-k25-0, river-9b, etc.)
- Tier-based routing (generate-t3, validate-t2, etc.)
- Legacy aliases (worker, supervisor, manager, lt-gov)
- Deterministic backend selection from task slug

Example:
    >>> from dgov.router import resolve, is_routable
    >>> resolve("generate-t3", slug="task-abc-123")
    'kimi-k25-2'  # deterministic from slug hash
    >>> is_routable("generate-t3")
    True
    >>> is_routable("unknown-route")
    False
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class PaneRole(StrEnum):
    """Pane role in the orchestration hierarchy."""

    WORKER = "worker"
    LT_GOV = "lt-gov"


class RoutingError(Exception):
    """Raised when routing fails."""

    pass


@dataclass(frozen=True)
class RouteConfig:
    """Configuration for a single routing entry."""

    name: str
    backends: tuple[str, ...]
    group: str | None = None
    escalates_to: str | None = None
    alias_for: str | None = None
    route_type: str | None = None
    alerts: tuple[str, ...] = ()

    @property
    def is_alias(self) -> bool:
        """True if this route is an alias to another route."""
        return self.alias_for is not None

    @property
    def is_launchable(self) -> bool:
        """True if this route has at least one backend."""
        return len(self.backends) > 0


class RoutingTable:
    """Loaded routing configuration from agents.toml."""

    def __init__(self, routes: dict[str, RouteConfig]):
        self._routes = routes

    def get(self, name: str) -> RouteConfig | None:
        """Get route config by name."""
        return self._routes.get(name)

    def is_routable(self, name: str) -> bool:
        """Check if a name maps to any routable entry (alias or backend-bearing)."""
        route = self._routes.get(name)
        if route is None:
            return False
        # Aliases are routable if they resolve to something routable
        if route.is_alias:
            target = self._routes.get(route.alias_for)
            if target is None:
                return False
            return target.is_launchable or target.is_alias
        # Direct routes need backends
        return route.is_launchable

    def resolve_chain(self, name: str, _seen: set[str] | None = None) -> RouteConfig:
        """Follow alias chain to final route config."""
        if _seen is None:
            _seen = set()
        if name in _seen:
            raise RoutingError(f"Circular alias detected: {name}")
        _seen.add(name)

        route = self._routes.get(name)
        if route is None:
            raise RoutingError(f"Unknown route: {name}")

        if route.is_alias:
            return self.resolve_chain(route.alias_for, _seen)

        return route

    def list_routes(self) -> list[str]:
        """List all defined route names."""
        return list(self._routes.keys())


# Cache for loaded routing tables (by project root)
_routing_cache: dict[str, RoutingTable] = {}


def _load_agents_toml(project_root: str) -> dict[str, Any]:
    """Load agents.toml from project root."""
    path = Path(project_root) / ".dgov" / "agents.toml"
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise RoutingError(f"Invalid TOML in agents.toml: {e}") from e
    except OSError as e:
        raise RoutingError(f"Cannot read agents.toml: {e}") from e


def _parse_routing_table(config: dict[str, Any]) -> RoutingTable:
    """Parse routing table from agents.toml config."""
    routes: dict[str, RouteConfig] = {}

    routing_section = config.get("routing", {})
    if not isinstance(routing_section, dict):
        raise RoutingError("[routing] section must be a table")

    for name, entry in routing_section.items():
        if not isinstance(entry, dict):
            continue

        # Handle alias_for
        alias_for = entry.get("alias_for")
        if alias_for is not None:
            routes[name] = RouteConfig(
                name=name,
                backends=(),
                alias_for=alias_for,
            )
            continue

        # Handle regular routing entry
        backends_raw = entry.get("backends", [])
        backends = tuple(backends_raw) if isinstance(backends_raw, list) else ()

        alerts_raw = entry.get("alerts", [])
        alerts = tuple(alerts_raw) if isinstance(alerts_raw, list) else ()

        routes[name] = RouteConfig(
            name=name,
            backends=backends,
            group=entry.get("group"),
            escalates_to=entry.get("escalates_to"),
            route_type=entry.get("type"),
            alerts=alerts,
        )

    return RoutingTable(routes)


def load_routing_table(project_root: str | None = None) -> RoutingTable:
    """Load routing table from agents.toml.

    Args:
        project_root: Project root directory (default: current directory)

    Returns:
        Loaded RoutingTable instance

    Raises:
        RoutingError: If agents.toml is malformed
    """
    if project_root is None:
        project_root = "."

    # Use cached version if available
    if project_root in _routing_cache:
        return _routing_cache[project_root]

    config = _load_agents_toml(project_root)
    table = _parse_routing_table(config)
    _routing_cache[project_root] = table
    return table


def clear_routing_cache() -> None:
    """Clear the routing table cache. Useful for testing."""
    _routing_cache.clear()


def _stable_choice(items: list[str], key: str) -> str:
    """Deterministically select one item from a list using a stable key.

    Uses hash of the key to ensure same key always selects same backend.
    """
    if not items:
        raise RoutingError("No backends available for selection")
    if len(items) == 1:
        return items[0]

    # Use hash for deterministic selection
    idx = hash(key) % len(items)
    return items[idx]


def resolve(
    agent: str,
    slug: str | None = None,
    project_root: str | None = None,
) -> str:
    """Resolve a logical agent name to a concrete backend.

    Args:
        agent: Logical agent name (e.g., "generate-t3", "worker", "kimi-k25-0")
        slug: Task slug for deterministic backend selection
        project_root: Project root for loading agents.toml

    Returns:
        Concrete backend name (e.g., "kimi-k25-2")

    Raises:
        RoutingError: If the route cannot be resolved to a launchable backend

    Examples:
        >>> resolve("generate-t3", slug="task-abc")
        'kimi-k25-2'  # deterministic from slug hash
        >>> resolve("worker", slug="task-xyz")
        'kimi-k25-0'  # worker -> generate-t3 -> kimis
        >>> resolve("kimi-k25-0", slug="any")
        'kimi-k25-0'  # already concrete
    """
    # Direct backend names pass through if they exist in agents.py
    from dgov.agents import get_agent

    agent_def = get_agent(agent)
    if agent_def is not None:
        return agent

    # Load routing table and resolve
    table = load_routing_table(project_root)

    # Check if this is a known route
    if not table.is_routable(agent):
        raise RoutingError(f"No routable entry for: {agent}")

    # Follow alias chain to final route
    route = table.resolve_chain(agent)

    # Fail fast if no backends
    if not route.is_launchable:
        raise RoutingError(
            f"Route '{agent}' resolves to '{route.name}' which has no launchable backends"
        )

    # Deterministic backend selection
    backends = list(route.backends)
    selection_key = slug or agent
    return _stable_choice(backends, selection_key)


def is_routable(agent: str, project_root: str | None = None) -> bool:
    """Check if an agent name is routable via routing tables.

    Args:
        agent: The agent or routing name to check
        project_root: Optional project root for loading routing config

    Returns:
        True if the agent can be routed to a backend
    """
    # First check if it's a direct backend in agents.py
    from dgov.agents import get_agent

    if get_agent(agent) is not None:
        return True

    # Otherwise check routing table
    try:
        table = load_routing_table(project_root)
        return table.is_routable(agent)
    except RoutingError:
        return False


def get_escalation_target(agent: str, project_root: str | None = None) -> str | None:
    """Get escalation target for a routed agent.

    Args:
        agent: The agent or routing name
        project_root: Optional project root for loading routing config

    Returns:
        Escalation route name or None if not configured
    """
    table = load_routing_table(project_root)

    try:
        route = table.resolve_chain(agent)
        return route.escalates_to
    except RoutingError:
        return None


def record_backend_failure(backend: str) -> None:
    """Record a backend failure for circuit breaker logic.

    Stub implementation - no-op for now.
    """
    pass
