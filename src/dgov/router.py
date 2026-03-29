"""Agent router: resolve logical model names to available physical backends.

Maps logical names (qwen-35b, qwen-9b, etc.) to ordered pools of physical
backends (river-35b, qwen35-35b, etc.). Checks health and concurrency to
pick the first available backend.

Includes circuit breaker for backends with recent failures.

Routing contract:
- Logical names come from project-local .dgov/agents.toml [routing.*] tables
- Falls back to user-global ~/.dgov/agents.toml if project-local is missing
- Never blocks on missing user-global config
- Degradation reasons are typed and deterministic
"""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import time
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# Type aliases for routing contract
LogicalName = str
BackendId = str
Role = str  # "worker" | "supervisor" | "manager" | "lt-gov"


# Degradation reason kinds (typed, frozen)
class DegradationReason(StrEnum):
    """Reasons why a backend candidate is unavailable."""

    NOT_REGISTERED = "not_registered"
    CIRCUIT_BREAKER = "circuit_breaker"
    GROUP_BLOCKED = "group_blocked"
    HEALTH_FAILURE = "health_failure"
    HEALTH_TIMEOUT = "health_timeout"
    CONCURRENT_LIMIT = "concurrent_limit"


class DegradationState(StrEnum):
    """Current state of degradation for a logical name resolution."""

    NONE = "none"
    FULL_FAILURE = "full_failure"


def _load_routing_tables(project_root: str | None = None) -> dict[LogicalName, list[BackendId]]:
    """Load routing tables from config files.

    Priority order (project-local takes precedence over user-global):
    1. Project-local: <project_root>/.dgov/agents.toml [routing.*]
    2. User global: ~/.dgov/agents.toml [routing.*]

    Returns {logical_name: [backend1, backend2, ...]}.
    """
    from dgov.agents import load_routing_tables

    return load_routing_tables(project_root)


def is_routable(name: LogicalName, project_root: str | None = None) -> bool:
    """Check if a name is a logical routing key.

    Returns True if the name is defined in routing tables (project-local or user-global).
    """
    tables = _load_routing_tables(project_root)
    return name in tables


def available_names() -> list[LogicalName]:
    """Return all logical routing names from config.

    Returns sorted list of logical names defined in routing tables.
    """
    tables = _load_routing_tables()
    return sorted(tables.keys())


def physical_to_logical(physical_name: str) -> LogicalName:
    """Map a physical backend name back to its logical routing name.

    Returns the physical name unchanged if no mapping is found.
    """
    tables = _load_routing_tables()
    for logical, backends in tables.items():
        if physical_name in backends:
            return logical
    return physical_name


def resolve_role(agent_name: str, project_root: str | None = None) -> Role:
    """Derive pane role from agent name.

    Returns "lt-gov" if agent_name is "lt-gov" or matches lt-gov routing backends.
    Uses project-local routing tables when available.
    Returns "worker" otherwise.
    """
    tables = _load_routing_tables(project_root)
    if agent_name == "lt-gov":
        return "lt-gov"
    lt_gov_backends = tables.get("lt-gov", [])
    if agent_name in lt_gov_backends:
        return "lt-gov"
    return "worker"


def record_backend_failure(session_root: str, backend_id: BackendId) -> None:
    """Record a backend failure and prune old entries.

    Appends current timestamp to JSON file at session_root/.dgov/backend_failures.json.
    Prunes all entries older than 10 minutes on each write.
    Uses a single read-modify-write critical section for concurrency safety.
    If file is unreadable/corrupt, silently resets to empty dict.
    """
    failures_file = Path(session_root) / ".dgov" / "backend_failures.json"
    failures_file.parent.mkdir(parents=True, exist_ok=True)

    now = time.time()

    try:
        with open(failures_file, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                data = json.load(f)
            except (json.JSONDecodeError, KeyError):
                data = {}

            if backend_id not in data:
                data[backend_id] = []
            data[backend_id].append(now)

            # Prune all backends' old entries
            for bid in list(data.keys()):
                data[bid] = [ts for ts in data[bid] if now - ts < 600]
                if not data[bid]:
                    del data[bid]

            f.seek(0)
            f.truncate()
            json.dump(data, f)
    except (OSError, IOError):
        pass


def _check_circuit_breaker(
    session_root: str, backend_id: BackendId, threshold: int = 2, window_minutes: int = 10
) -> bool:
    """Check if circuit breaker is tripped for a backend.

    Returns True if backend has >= threshold failures within the last window_minutes.
    Returns False if file missing, unreadable, or backend not in file.
    Never raises — fails open (returns False on any error).
    """
    failures_file = Path(session_root) / ".dgov" / "backend_failures.json"

    try:
        with open(failures_file, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
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
    name: LogicalName,
    session_root: str,
    project_root: str,
) -> tuple[BackendId, LogicalName]:
    """Resolve a logical agent name to an available physical backend.

    Returns (physical_agent_id, logical_name).
    If name is not a logical routing key, returns (name, name) unchanged.
    Checks health and concurrency for each backend in order.

    Degradation is typed and deterministic:
    - When all candidates fail, raises DegradationError with typed reasons
    - Each failure is categorized by DegradationReason
    - DegradationState indicates whether a full failure occurred
    """
    from dgov.agents import load_groups, load_registry
    from dgov.backend import get_backend
    from dgov.persistence import PaneState, all_panes
    from dgov.status import _count_active_agent_workers

    # Load routing tables (project-local takes precedence)
    tables = _load_routing_tables(project_root)

    # Check if name is a logical routing key
    if name not in tables:
        # Not a routing key - return as-is (passthrough for physical agent names)
        return name, name

    backends = tables[name]
    registry = load_registry(project_root)
    groups = load_groups(project_root)

    # Optimization: fetch all active panes and tmux info once for group checks
    _TERMINAL_STATES = {
        PaneState.DONE,
        PaneState.FAILED,
        PaneState.SUPERSEDED,
        PaneState.MERGED,
        PaneState.CLOSED,
        PaneState.ESCALATED,
        PaneState.TIMED_OUT,
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

    backend_failures: dict[BackendId, list[DegradationReason]] = {}
    tried: list[tuple[BackendId, DegradationReason]] = []
    degraded_candidates: list[BackendId] = []
    viable: list[tuple[BackendId, int, int]] = []  # (backend_id, active_count, original_index)
    for backend_id in backends:
        agent_def = registry.get(backend_id)
        if agent_def is None:
            tried.append((backend_id, DegradationReason.NOT_REGISTERED))
            backend_failures[backend_id] = [DegradationReason.NOT_REGISTERED]
            continue

        # Circuit breaker check
        if _check_circuit_breaker(session_root, backend_id):
            tried.append((backend_id, DegradationReason.CIRCUIT_BREAKER))
            backend_failures[backend_id] = backend_failures.get(backend_id, []) + [
                DegradationReason.CIRCUIT_BREAKER
            ]
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
                        tried.append((backend_id, DegradationReason.GROUP_BLOCKED))
                        backend_failures[backend_id] = backend_failures.get(backend_id, []) + [
                            DegradationReason.GROUP_BLOCKED
                        ]
                        group_blocked = True
                        break
            if group_blocked:
                continue

        # Health check
        if agent_def.health.check:
            try:
                hc = subprocess.run(
                    agent_def.health.check,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if hc.returncode != 0:
                    tried.append((backend_id, DegradationReason.HEALTH_FAILURE))
                    backend_failures[backend_id] = backend_failures.get(backend_id, []) + [
                        DegradationReason.HEALTH_FAILURE
                    ]
                    degraded_candidates.append(backend_id)
                    continue
            except (subprocess.TimeoutExpired, OSError):
                tried.append((backend_id, DegradationReason.HEALTH_TIMEOUT))
                backend_failures[backend_id] = backend_failures.get(backend_id, []) + [
                    DegradationReason.HEALTH_TIMEOUT
                ]
                degraded_candidates.append(backend_id)
                continue

        # Individual Concurrency check
        max_concurrent = agent_def.max_concurrent
        if max_concurrent is not None:
            active = _count_active_agent_workers(session_root, backend_id)
            if active >= max_concurrent:
                tried.append((backend_id, DegradationReason.CONCURRENT_LIMIT))
                backend_failures[backend_id] = backend_failures.get(backend_id, []) + [
                    DegradationReason.CONCURRENT_LIMIT
                ]
                continue

        # Success - collect this backend for least-loaded selection
        active = _count_active_agent_workers(session_root, backend_id)
        viable.append((backend_id, active, len(viable)))
        logger.info("Routed %s -> %s (active=%d)", name, backend_id, active)
        continue

    # After checking all backends, pick least-loaded viable backend
    if viable:
        best = min(viable, key=lambda t: (t[1], t[2]))
        logger.info("Selected least-loaded backend %s for %s", best[0], name)
        return best[0], name

    if degraded_candidates:
        backend_id = degraded_candidates[0]
        logger.warning(
            "Routing %s degraded to %s because all healthy backends were unavailable",
            name,
            backend_id,
        )
        return backend_id, name

    raise DegradationError(tried, backend_failures)


class DegradationError(Exception):
    """Raised when all backend candidates for a logical name are unavailable."""

    def __init__(
        self,
        tried: list[tuple[BackendId, DegradationReason]],
        failures: dict[BackendId, list[DegradationReason]],
    ):
        super().__init__(self._build_message(tried, failures))
        self.tried = tried
        self.failures: dict[BackendId, list[DegradationReason]] = failures

    @staticmethod
    def _build_message(
        tried: list[tuple[BackendId, DegradationReason]],
        failures: dict[BackendId, list[DegradationReason]],
    ) -> str:
        """Build a deterministic error message from typed degradation reasons."""
        lines: list[str] = []

        for backend_id, reason in tried:
            lines.append(f"  - {backend_id} ({reason.value})")

        lines.append("")
        lines.append("Degradation summary:")

        for backend_id, reasons in failures.items():
            if reasons:
                reason_values = sorted({reason.value for reason in reasons})
                lines.append(f"  {backend_id}: {', '.join(reason_values)}")

        return "\n".join(lines)

    def get_state(self) -> DegradationState:
        """Return the current degradation state."""
        return DegradationState.FULL_FAILURE if self.tried else DegradationState.NONE

    def get_reasons(self) -> list[DegradationReason]:
        """Return all unique degradation reasons encountered."""
        reasons: set[DegradationReason] = set()
        for _, reason in self.tried:
            reasons.add(reason)
        for backend_reasons in self.failures.values():
            reasons.update(backend_reasons)
        return sorted(reasons)

    def has_full_failure(self) -> bool:
        """Check if all backends have at least one degradation reason."""
        return bool(self.tried)
