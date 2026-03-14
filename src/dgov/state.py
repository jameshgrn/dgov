# state management
"""State module — reads .dgov/state.json."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor

from dgov.panes import list_worker_panes

_TUNNEL_PORTS = (8080, 8081, 8082, 8083)
_HEALTH_TIMEOUT = 2


def _check_single_port(port: int) -> tuple[int, str]:
    """Check a single port and return (port, "up"|"down")."""
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
                str(_HEALTH_TIMEOUT),
                f"http://localhost:{port}/health",
            ],
            capture_output=True,
            text=True,
            timeout=_HEALTH_TIMEOUT + 2,
        )
        return port, ("up" if result.stdout.strip() == "200" else "down")
    except (subprocess.TimeoutExpired, OSError):
        return port, "down"


def _check_tunnel_health() -> dict:
    """Check SSH tunnel health by probing each llama.cpp port in parallel.

    Returns {"ports": {8080: "up"|"down", ...}, "any_up": bool}.
    """
    ports: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=len(_TUNNEL_PORTS)) as executor:
        results = list(executor.map(_check_single_port, _TUNNEL_PORTS))

    for port, status in results:
        ports[port] = status

    return {"ports": ports, "any_up": any(v == "up" for v in ports.values())}


def _check_kerberos_ticket() -> dict:
    """Check Kerberos ticket status via klist.

    Returns {"valid": bool, "principal": str|None, "expires": str|None}.
    """
    try:
        result = subprocess.run(
            ["klist", "--test"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        has_ticket = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"valid": False, "principal": None, "expires": None}

    if not has_ticket:
        return {"valid": False, "principal": None, "expires": None}

    # Parse klist output for principal and expiry
    # Heimdal (macOS): "        Principal: user@REALM"
    # MIT: "Default principal: user@REALM"
    # Expiry line: "Mar  5 15:17:55 2026  krbtgt/REALM@REALM"
    principal = None
    expires = None
    try:
        detail = subprocess.run(
            ["klist"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in detail.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Principal:") or stripped.startswith("Default principal:"):
                principal = stripped.split(":", 1)[1].strip()
            if "krbtgt/" in line:
                # Extract the Expires column (second date group)
                # Format: "Mar  5 05:17:57 2026  Mar  5 15:17:55 2026  krbtgt/..."
                parts = line.split()
                # Find "krbtgt" index, expiry is the 4 tokens before it
                for idx, p in enumerate(parts):
                    if p.startswith("krbtgt/"):
                        if idx >= 4:
                            expires = " ".join(parts[idx - 4 : idx])
                        break
    except (subprocess.TimeoutExpired, OSError):
        pass

    return {"valid": True, "principal": principal, "expires": expires}


def get_status(project_root: str, session_root: str | None = None) -> dict:
    """Get full dgov status as JSON-serializable dict."""
    panes = list_worker_panes(project_root, session_root=session_root)
    return {
        "panes": panes,
        "total": len(panes),
        "alive": sum(1 for p in panes if p["alive"]),
        "tunnel": _check_tunnel_health(),
        "kerberos": _check_kerberos_ticket(),
    }
