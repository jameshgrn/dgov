"""Pre-flight validation for dgov dispatch.

Runs all checks before spawning worker panes and optionally auto-fixes
fixable failures (stale worktrees, deps, agent health).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dgov.agents import AGENT_REGISTRY

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    critical: bool
    message: str
    fixable: bool = False


@dataclass
class PreflightReport:
    checks: list[CheckResult]
    timestamp: str = field(init=False)

    def __post_init__(self) -> None:
        self.timestamp = datetime.now(timezone.utc).isoformat()

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.critical)

    def to_dict(self) -> dict:
        return {
            "checks": [asdict(c) for c in self.checks],
            "passed": self.passed,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Individual checkers
# ---------------------------------------------------------------------------


def _normalize_touch_path(path: str) -> str:
    return path.strip().lstrip("./").rstrip("/")


def _paths_overlap(path: str, touch: str) -> bool:
    norm_path = _normalize_touch_path(path)
    norm_touch = _normalize_touch_path(touch)
    if not norm_path or not norm_touch:
        return False
    return norm_path == norm_touch or norm_path.startswith(norm_touch + "/")


def check_agent_cli(agent: str, *, registry: dict | None = None) -> CheckResult:
    """Check that the agent CLI binary is on PATH.

    Handles both physical agent names (in registry) and logical routing
    names (resolved via router). Logical names are valid if routable.
    """
    reg = registry or AGENT_REGISTRY
    defn = reg.get(agent)
    if defn is None:
        # Check if it's a logical routing name (e.g. qwen-35b, worker, supervisor)
        from dgov.router import is_routable

        if is_routable(agent):
            return CheckResult(
                name="agent_cli",
                passed=True,
                critical=True,
                message=f"Logical agent '{agent}' is routable",
            )
        return CheckResult(
            name="agent_cli",
            passed=False,
            critical=True,
            message=f"Unknown agent '{agent}' -- not in registry or routing tables",
        )
    cmd = defn.prompt_command.split()[0]
    found = shutil.which(cmd) is not None
    return CheckResult(
        name="agent_cli",
        passed=found,
        critical=True,
        message=f"{cmd} found on PATH" if found else f"{cmd} not found on PATH",
    )


def check_git_clean(project_root: str, touches: list[str] | None = None) -> CheckResult:
    """Check for uncommitted changes to tracked files."""
    root = Path(project_root).resolve()
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if dirty.returncode == 128:
            return CheckResult(
                name="git_clean",
                passed=True,
                critical=True,
                message="Not a git repo or no commits -- skipped",
            )
        if dirty.returncode != 0:
            return CheckResult(
                name="git_clean",
                passed=False,
                critical=True,
                message="git status failed",
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="git_clean",
            passed=False,
            critical=True,
            message=f"git check failed: {exc}",
        )
    dirty_files: set[str] = set()
    for line in dirty.stdout.splitlines():
        if len(line) < 4:
            continue
        if line.startswith("??"):
            continue
        dirty_files.add(line[3:])

    if not dirty_files:
        return CheckResult(
            name="git_clean",
            passed=True,
            critical=True,
            message="Working tree clean",
        )

    if touches:
        overlapping = sorted(
            f for f in dirty_files if any(_paths_overlap(f, touch) for touch in touches)
        )
        if not overlapping:
            return CheckResult(
                name="git_clean",
                passed=True,
                critical=True,
                message="Repo has unrelated tracked changes outside declared touches",
            )
        return CheckResult(
            name="git_clean",
            passed=False,
            critical=True,
            message=f"Repo has tracked changes overlapping touches: {', '.join(overlapping[:5])}",
        )

    return CheckResult(
        name="git_clean",
        passed=False,
        critical=True,
        message="Repo has tracked changes",
    )


def check_git_branch(project_root: str, expected: str | None = None) -> CheckResult:
    """Check which branch HEAD is on, optionally compare to expected."""
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="git_branch",
            passed=False,
            critical=False,
            message=f"Could not determine branch: {exc}",
        )
    if expected and branch != expected:
        return CheckResult(
            name="git_branch",
            passed=False,
            critical=False,
            message=f"On branch '{branch}', expected '{expected}'",
        )
    return CheckResult(
        name="git_branch",
        passed=True,
        critical=False,
        message=f"On branch '{branch}'"
        + (
            " (matches expected)"
            if expected
            else (" (not main)" if branch not in ("main", "master", "HEAD") else "")
        ),
    )


def check_deps(project_root: str) -> CheckResult:
    """Verify installed deps match pyproject.toml via uv."""
    try:
        result = subprocess.run(
            ["uv", "sync", "--locked"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # --locked asserts the lockfile won't change. Non-zero exit = out of sync.
        if result.returncode != 0:
            return CheckResult(
                name="deps",
                passed=False,
                critical=False,
                message=f"Dependency check failed: {result.stderr.strip()[:200]}",
                fixable=True,
            )
    except FileNotFoundError:
        return CheckResult(
            name="deps",
            passed=False,
            critical=False,
            message="uv not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="deps",
            passed=False,
            critical=False,
            message="uv sync --locked timed out",
        )
    return CheckResult(
        name="deps",
        passed=True,
        critical=False,
        message="Dependencies in sync",
        fixable=True,
    )


def check_stale_worktrees(project_root: str) -> CheckResult:
    """Flag git worktrees with no matching pane in state."""
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        worktrees: list[str] = []
        is_first = True
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = line.split(" ", 1)[1]
                # First entry in porcelain output is always the main worktree
                if is_first:
                    is_first = False
                    continue
                worktrees.append(wt_path)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="stale_worktrees",
            passed=True,
            critical=False,
            message=f"Could not list worktrees: {exc}",
        )

    if not worktrees:
        return CheckResult(
            name="stale_worktrees",
            passed=True,
            critical=False,
            message="No extra worktrees found",
            fixable=True,
        )

    # Check which worktrees have matching pane state
    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, include_freshness=False)
    pane_worktrees = {p.get("worktree_path") for p in panes}
    stale = [wt for wt in worktrees if wt not in pane_worktrees]

    if stale:
        return CheckResult(
            name="stale_worktrees",
            passed=False,
            critical=False,
            message=f"{len(stale)} stale worktree(s): {', '.join(stale[:3])}",
            fixable=True,
        )
    return CheckResult(
        name="stale_worktrees",
        passed=True,
        critical=False,
        message=f"{len(worktrees)} worktree(s), all tracked",
        fixable=True,
    )


def check_file_locks(project_root: str, touches: list[str]) -> CheckResult:
    """Check if any touched files have conflicts with active panes."""
    if not touches:
        return CheckResult(
            name="file_locks",
            passed=True,
            critical=False,
            message="No file touches declared — claim conflicts unverifiable",
        )

    from dgov.persistence import all_panes

    root = Path(project_root).resolve()
    panes = all_panes(project_root)
    conflicts: list[str] = []

    for pane in panes:
        wt = pane.get("worktree_path")
        base_sha = pane.get("base_sha", "")
        pane_state = pane.get("state", "")

        # Skip terminal-state panes — they're no longer working
        if pane_state in (
            "done",
            "failed",
            "merged",
            "closed",
            "abandoned",
            "superseded",
            "timed_out",
            "escalated",
        ):
            continue

        try:
            # First: check declared claims against declared claims (fast, no git)
            pane_claims = pane.get("file_claims") or []
            if isinstance(pane_claims, str):
                import json as _json

                try:
                    pane_claims = _json.loads(pane_claims)
                except (ValueError, TypeError):
                    pane_claims = []

            claim_overlap = {
                touch
                for touch in touches
                if any(_paths_overlap(str(c), touch) for c in pane_claims)
            }
            if claim_overlap:
                conflicts.append(f"{pane['slug']} (claimed): {', '.join(sorted(claim_overlap))}")
                continue  # no need to also check git state

            # Second: check actual changed files (slower, needs git)
            changed: set[str] = set()
            if wt and Path(wt).exists() and base_sha:
                committed = subprocess.run(
                    ["git", "diff", "--name-only", f"{base_sha}..HEAD"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if committed.returncode == 0:
                    changed.update(
                        path for path in committed.stdout.strip().splitlines() if path.strip()
                    )

            if wt and Path(wt).exists():
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if status.returncode == 0:
                    for line in status.stdout.splitlines():
                        if len(line) < 4:
                            continue
                        changed.add(line[3:])

            if changed:
                overlap = {
                    path
                    for path in changed
                    if any(_paths_overlap(path, touch) for touch in touches)
                }
                if overlap:
                    conflicts.append(f"{pane['slug']}: {', '.join(sorted(overlap))}")
        except (subprocess.TimeoutExpired, OSError):
            continue

    # Also check for .lock files
    for f in touches:
        lock = root / f"{f}.lock"
        if lock.exists():
            conflicts.append(f"lock file: {lock}")

    if conflicts:
        return CheckResult(
            name="file_locks",
            passed=False,
            critical=True,
            message=f"File conflicts: {'; '.join(conflicts[:5])}",
        )
    return CheckResult(
        name="file_locks",
        passed=True,
        critical=True,
        message="No file conflicts detected",
    )


def check_agent_concurrency(
    project_root: str,
    agent: str,
    session_root: str | None = None,
    *,
    registry: dict | None = None,
) -> CheckResult:
    """Check if spawning another worker would exceed the agent's max_concurrent."""
    from dgov.agents import load_registry

    reg = registry or load_registry(project_root)
    agent_def = reg.get(agent)
    if not agent_def or agent_def.max_concurrent is None:
        return CheckResult(
            name="agent_concurrency",
            passed=True,
            critical=False,
            message=f"No concurrency limit for {agent}",
        )
    from dgov.status import _count_active_agent_workers

    session_root_resolved = os.path.abspath(session_root or project_root)
    active = _count_active_agent_workers(session_root_resolved, agent)
    limit = agent_def.max_concurrent
    if active >= limit:
        return CheckResult(
            name="agent_concurrency",
            passed=False,
            critical=True,
            message=f"{active} {agent} workers running (max {limit})",
        )
    return CheckResult(
        name="agent_concurrency",
        passed=True,
        critical=True,
        message=f"{active} {agent} workers running (max {limit})",
    )


def check_agent_health(
    agent: str,
    *,
    registry: dict | None = None,
    project_root: str | None = None,
) -> CheckResult:
    """Run the agent's health_check command if configured."""
    from dgov.agents import load_registry

    reg = registry or load_registry(project_root)
    agent_def = reg.get(agent)
    if not agent_def or not agent_def.health_check:
        return CheckResult(
            name="agent_health",
            passed=True,
            critical=False,
            message=f"No health check for {agent}",
        )
    try:
        result = subprocess.run(
            agent_def.health_check,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return CheckResult(
                name="agent_health",
                passed=True,
                critical=True,
                message=f"Health check passed for {agent}",
                fixable=bool(agent_def.health_fix),
            )
        return CheckResult(
            name="agent_health",
            passed=False,
            critical=True,
            message=f"Health check failed for {agent}: {agent_def.health_check}",
            fixable=bool(agent_def.health_fix),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="agent_health",
            passed=False,
            critical=True,
            message=f"Health check error for {agent}: {exc}",
            fixable=bool(agent_def.health_fix),
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def check_river_tunnel() -> CheckResult:
    """Check if the multiplexed River SSH tunnel is active."""
    socket = Path.home() / ".dgov" / "river.sock"
    if not socket.exists():
        # Fallback to /tmp/river.sock
        socket = Path("/tmp/river.sock")

    if not socket.exists():
        return CheckResult(
            name="river_tunnel",
            passed=False,
            critical=False,
            message="River SSH tunnel socket not found",
            fixable=True,
        )

    # Check if the socket is alive
    try:
        result = subprocess.run(
            ["ssh", "-S", str(socket), "-O", "check", "river.emes.unc.edu"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                name="river_tunnel",
                passed=True,
                critical=False,
                message="River multiplexed tunnel is alive",
            )
        else:
            return CheckResult(
                name="river_tunnel",
                passed=False,
                critical=False,
                message="River tunnel socket exists but connection is dead",
                fixable=True,
            )
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult(
            name="river_tunnel",
            passed=False,
            critical=False,
            message="Timed out checking river tunnel",
            fixable=True,
        )


def run_preflight(
    project_root: str,
    agent: str = "claude",
    touches: list[str] | None = None,
    expected_branch: str | None = None,
    session_root: str | None = None,
    *,
    skip_deps: bool = True,
) -> PreflightReport:
    """Run all pre-flight checks and return a structured report.

    Args:
        skip_deps: Skip the heavyweight ``uv sync --locked`` check (default True).
            Run ``dgov preflight`` explicitly to include it.
    """
    from dgov.agents import load_registry

    registry = load_registry(project_root)
    checks: list[CheckResult] = []

    checks.append(check_agent_cli(agent, registry=registry))
    if agent.startswith("river-") or "river" in agent:
        checks.append(check_river_tunnel())
    checks.append(check_git_clean(project_root, touches=touches))
    checks.append(check_git_branch(project_root, expected=expected_branch))

    # Config-driven health check for agents with custom health_check
    agent_def = registry.get(agent)
    if agent_def and agent_def.health_check:
        checks.append(check_agent_health(agent, registry=registry, project_root=project_root))

    checks.append(check_agent_concurrency(project_root, agent, session_root, registry=registry))

    if not skip_deps:
        checks.append(check_deps(project_root))
    checks.append(check_stale_worktrees(project_root))
    checks.append(check_file_locks(project_root, touches or []))

    return PreflightReport(checks=checks)


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


def _fix_deps(project_root: str) -> bool:
    """Run uv sync to fix dependency mismatches."""
    try:
        result = subprocess.run(
            ["uv", "sync"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _fix_stale_worktrees(project_root: str) -> bool:
    """Prune git worktrees that are no longer on disk."""
    try:
        result = subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_FIXER_NAMES = {"deps", "stale_worktrees", "agent_health", "river_tunnel"}


def _fix_river_tunnel(project_root: str) -> bool:
    """Start the river SSH tunnel via shell."""
    try:
        shell = shutil.which("zsh") or shutil.which("bash")
        if shell:
            subprocess.run(
                [shell, "-c", f"source ~/{shell.split('/')[-1]}rc && river-tunnel"],
                timeout=30,
            )
            return True
    except Exception:
        logger.debug("Failed to start river-tunnel via shell fix attempt", exc_info=True)
    return False


def _fix_agent_health(project_root: str, agent_id: str | None = None) -> bool:
    """Run the failing agent's health_fix command.

    Args:
        agent_id: The specific agent whose health_fix to run.  When *None*,
            falls back to trying every agent with a health_fix (legacy).
    """
    from dgov.agents import load_registry

    registry = load_registry(project_root)

    if agent_id is not None:
        agent_def = registry.get(agent_id)
        if not agent_def or not agent_def.health_fix:
            return False
        try:
            result = subprocess.run(
                agent_def.health_fix,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    # Fallback: no agent_id provided — try all (legacy path)
    for defn in registry.values():
        if defn.health_fix:
            try:
                result = subprocess.run(
                    defn.health_fix,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    return True
            except (subprocess.TimeoutExpired, OSError):
                continue
    return False


# Registry mapping fixer names to (fix function, check function) tuples.
FIXER_REGISTRY: dict[str, tuple] = {
    "stale_worktrees": (_fix_stale_worktrees, check_stale_worktrees),
    "deps": (_fix_deps, check_deps),
    "agent_health": (_fix_agent_health, check_agent_health),
    "river_tunnel": (_fix_river_tunnel, check_river_tunnel),
}


def fix_preflight(report: PreflightReport, project_root: str) -> PreflightReport:
    """Auto-fix fixable failures, then re-run those checks."""
    recheck: list[str] = []
    for check in report.checks:
        if not check.passed and check.fixable and check.name in _FIXER_NAMES:
            fixer, _ = FIXER_REGISTRY[check.name]
            # river_tunnel uses a special fix function that doesn't take project_root
            if check.name == "river_tunnel":
                try:
                    if fixer():
                        recheck.append(check.name)
                except Exception:
                    logger.debug(
                        "Failed to start river-tunnel via shell fix attempt", exc_info=True
                    )
            else:
                if fixer(project_root):
                    recheck.append(check.name)

    if not recheck:
        return report

    # Re-run only the fixed checks
    new_checks = []
    for check in report.checks:
        if check.name in recheck:
            _, checker = FIXER_REGISTRY[check.name]
            # river_tunnel has special recheck logic - also re-run stale_worktrees and agent_health
            if check.name == "river_tunnel":
                new_checks.append(checker())
                new_checks.append(check_stale_worktrees(project_root))

                # Re-run all agent health checks
                from dgov.agents import load_registry

                registry = load_registry(project_root)
                for agent_id, defn in registry.items():
                    if defn.health_check:
                        new_checks.append(
                            check_agent_health(
                                agent_id, registry=registry, project_root=project_root
                            )
                        )
                        break  # only one agent_health check in the report
            else:
                new_checks.append(checker(project_root))
        else:
            new_checks.append(check)

    return PreflightReport(checks=new_checks)
