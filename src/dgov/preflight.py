"""Pre-flight validation for dgov dispatch.

Runs all checks before spawning worker panes and optionally auto-fixes
fixable failures (tunnel down, kerberos expired, stale worktrees, deps).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dgov.agents import AGENT_REGISTRY


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
    passed: bool = field(init=False)
    timestamp: str = field(init=False)

    def __post_init__(self) -> None:
        self.passed = all(c.passed for c in self.checks if c.critical)
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "checks": [asdict(c) for c in self.checks],
            "passed": self.passed,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Individual checkers
# ---------------------------------------------------------------------------

_TUNNEL_PORTS_DEFAULT = (8080, 8081, 8082)
_TUNNEL_TIMEOUT = 3


def check_agent_cli(agent: str) -> CheckResult:
    """Check that the agent CLI binary is on PATH."""
    defn = AGENT_REGISTRY.get(agent)
    if defn is None:
        return CheckResult(
            name="agent_cli",
            passed=False,
            critical=True,
            message=f"Unknown agent '{agent}' -- not in AGENT_REGISTRY",
        )
    cmd = defn.prompt_command.split()[0]
    found = shutil.which(cmd) is not None
    return CheckResult(
        name="agent_cli",
        passed=found,
        critical=True,
        message=f"{cmd} found on PATH" if found else f"{cmd} not found on PATH",
    )


def check_tunnel(ports: tuple[int, ...] = _TUNNEL_PORTS_DEFAULT) -> CheckResult:
    """Curl health-check each llama.cpp port."""
    up: list[int] = []
    down: list[int] = []
    for port in ports:
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--max-time",
                    str(_TUNNEL_TIMEOUT),
                    f"http://localhost:{port}/health",
                ],
                capture_output=True,
                text=True,
                timeout=_TUNNEL_TIMEOUT + 2,
            )
            if result.stdout.strip() == "200":
                up.append(port)
            else:
                down.append(port)
        except (subprocess.TimeoutExpired, OSError):
            down.append(port)

    passed = len(up) > 0
    detail = f"up={up} down={down}" if down else f"all ports up: {up}"
    return CheckResult(
        name="tunnel",
        passed=passed,
        critical=True,
        message=f"SSH tunnel: {detail}",
        fixable=True,
    )


def check_git_clean(project_root: str) -> CheckResult:
    """Check for uncommitted changes to tracked files."""
    root = Path(project_root).resolve()
    try:
        unstaged = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"],
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        if unstaged.returncode == 128:
            return CheckResult(
                name="git_clean",
                passed=True,
                critical=True,
                message="Not a git repo or no commits -- skipped",
            )
        if unstaged.returncode != 0:
            return CheckResult(
                name="git_clean",
                passed=False,
                critical=True,
                message="Repo has unstaged changes to tracked files",
            )
        staged = subprocess.run(
            ["git", "diff", "--quiet", "--cached"],
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        if staged.returncode != 0:
            return CheckResult(
                name="git_clean",
                passed=False,
                critical=True,
                message="Repo has staged but uncommitted changes",
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="git_clean",
            passed=False,
            critical=True,
            message=f"git check failed: {exc}",
        )
    return CheckResult(
        name="git_clean",
        passed=True,
        critical=True,
        message="Working tree clean",
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
            passed=True,
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
        message=f"On branch '{branch}'" + (" (matches expected)" if expected else ""),
    )


def check_kerberos(min_remaining_hours: int = 2) -> CheckResult:
    """Check Kerberos ticket validity and remaining lifetime."""
    try:
        result = subprocess.run(
            ["klist", "--test"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return CheckResult(
                name="kerberos",
                passed=False,
                critical=True,
                message="No valid Kerberos ticket",
                fixable=True,
            )
    except FileNotFoundError:
        return CheckResult(
            name="kerberos",
            passed=False,
            critical=True,
            message="klist not found -- Kerberos not installed",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="kerberos",
            passed=False,
            critical=True,
            message="klist timed out",
        )

    # Ticket exists -- try to parse expiry for remaining time check
    try:
        detail = subprocess.run(
            ["klist"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in detail.stdout.splitlines():
            if "krbtgt/" in line:
                parts = line.split()
                for idx, p in enumerate(parts):
                    if p.startswith("krbtgt/"):
                        if idx >= 4:
                            expiry_str = " ".join(parts[idx - 4 : idx])
                            # Try common klist date formats
                            for fmt in (
                                "%b %d %H:%M:%S %Y",  # "Mar  5 15:17:55 2026"
                                "%m/%d/%Y %H:%M:%S",  # "03/05/2026 15:17:55"
                            ):
                                try:
                                    expiry = datetime.strptime(expiry_str, fmt)
                                    remaining = expiry - datetime.now()
                                    hours_left = remaining.total_seconds() / 3600
                                    if hours_left < min_remaining_hours:
                                        return CheckResult(
                                            name="kerberos",
                                            passed=False,
                                            critical=True,
                                            message=(
                                                f"Kerberos ticket expires in "
                                                f"{hours_left:.1f}h "
                                                f"(min {min_remaining_hours}h)"
                                            ),
                                            fixable=True,
                                        )
                                    return CheckResult(
                                        name="kerberos",
                                        passed=True,
                                        critical=True,
                                        message=(
                                            f"Kerberos ticket valid, {hours_left:.1f}h remaining"
                                        ),
                                        fixable=True,
                                    )
                                except ValueError:
                                    continue
                        break
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Could not parse expiry but ticket is valid per --test
    return CheckResult(
        name="kerberos",
        passed=True,
        critical=True,
        message="Kerberos ticket valid (could not parse expiry)",
        fixable=True,
    )


def check_deps() -> CheckResult:
    """Verify installed deps match pyproject.toml via uv."""
    try:
        result = subprocess.run(
            ["uv", "sync", "--locked"],
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
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = line.split(" ", 1)[1]
                # Skip the main worktree (resolve both to handle symlinks)
                if str(Path(wt_path).resolve()) != str(root):
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
    from dgov.panes import list_worker_panes

    panes = list_worker_panes(project_root)
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
            critical=True,
            message="No file touches declared",
        )

    from dgov.panes import list_worker_panes

    root = Path(project_root).resolve()
    panes = list_worker_panes(project_root)
    conflicts: list[str] = []

    for pane in panes:
        wt = pane.get("worktree_path")
        if not wt or not Path(wt).exists():
            continue
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=wt,
                capture_output=True,
                text=True,
                timeout=10,
            )
            changed = set(result.stdout.strip().splitlines())
            overlap = changed & set(touches)
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


def check_gpu_concurrency(
    project_root: str, agent: str, session_root: str | None = None
) -> CheckResult:
    """Check if spawning another pi worker would exceed GPU limits."""
    if agent != "pi":
        return CheckResult(
            name="gpu_concurrency",
            passed=True,
            critical=False,
            message="Not a pi worker, GPU check skipped",
        )
    from dgov.panes import _MAX_CONCURRENT_PI_WORKERS, _count_active_pi_workers

    session_root_resolved = os.path.abspath(session_root or project_root)
    active = _count_active_pi_workers(session_root_resolved)
    if active >= _MAX_CONCURRENT_PI_WORKERS:
        return CheckResult(
            name="gpu_concurrency",
            passed=False,
            critical=True,
            message=f"{active} pi workers running (max {_MAX_CONCURRENT_PI_WORKERS})",
        )
    return CheckResult(
        name="gpu_concurrency",
        passed=True,
        critical=True,
        message=f"{active} pi workers running (max {_MAX_CONCURRENT_PI_WORKERS})",
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# Agents that require the SSH tunnel to river
_TUNNEL_AGENTS = {"pi"}
# Agents that require Kerberos (tunnel auth)
_KERBEROS_AGENTS = {"pi"}


def run_preflight(
    project_root: str,
    agent: str = "claude",
    touches: list[str] | None = None,
    expected_branch: str | None = None,
    session_root: str | None = None,
) -> PreflightReport:
    """Run all pre-flight checks and return a structured report."""
    checks: list[CheckResult] = []

    checks.append(check_agent_cli(agent))
    checks.append(check_git_clean(project_root))
    checks.append(check_git_branch(project_root, expected=expected_branch))

    if agent in _TUNNEL_AGENTS:
        checks.append(check_tunnel())
    if agent in _KERBEROS_AGENTS:
        checks.append(check_kerberos())

    checks.append(check_gpu_concurrency(project_root, agent, session_root))

    checks.append(check_deps())
    checks.append(check_stale_worktrees(project_root))
    checks.append(check_file_locks(project_root, touches or []))

    return PreflightReport(checks=checks)


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


def _fix_tunnel() -> bool:
    """Bring up the SSH tunnel to river."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-N",
                "-f",
                "-L",
                "8080:localhost:8080",
                "-L",
                "8081:localhost:8081",
                "-L",
                "8082:localhost:8082",
                "-L",
                "8083:localhost:8083",
                "-o",
                "ServerAliveInterval=60",
                "-o",
                "ServerAliveCountMax=3",
                "jgearon@river.emes.unc.edu",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _fix_kerberos() -> bool:
    """Attempt to renew Kerberos ticket via kinit."""

    pw = os.environ.get("RIVER_PW")
    if not pw:
        return False
    try:
        proc = subprocess.run(
            ["kinit", "jgearon@AD.UNC.EDU"],
            input=pw + "\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _fix_deps() -> bool:
    """Run uv sync to fix dependency mismatches."""
    try:
        result = subprocess.run(
            ["uv", "sync"],
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


_FIXER_NAMES = {"tunnel", "kerberos", "deps", "stale_worktrees"}


def fix_preflight(report: PreflightReport, project_root: str) -> PreflightReport:
    """Auto-fix fixable failures, then re-run those checks."""
    recheck: list[str] = []
    for check in report.checks:
        if not check.passed and check.fixable and check.name in _FIXER_NAMES:
            if check.name == "stale_worktrees":
                if _fix_stale_worktrees(project_root):
                    recheck.append(check.name)
            elif check.name == "tunnel":
                if _fix_tunnel():
                    recheck.append(check.name)
            elif check.name == "kerberos":
                if _fix_kerberos():
                    recheck.append(check.name)
            elif check.name == "deps":
                if _fix_deps():
                    recheck.append(check.name)

    if not recheck:
        return report

    # Re-run only the fixed checks
    new_checks = []
    for check in report.checks:
        if check.name in recheck:
            if check.name == "tunnel":
                new_checks.append(check_tunnel())
            elif check.name == "kerberos":
                new_checks.append(check_kerberos())
            elif check.name == "deps":
                new_checks.append(check_deps())
            elif check.name == "stale_worktrees":
                new_checks.append(check_stale_worktrees(project_root))
            else:
                new_checks.append(check)
        else:
            new_checks.append(check)

    return PreflightReport(checks=new_checks)
