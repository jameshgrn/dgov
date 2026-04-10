"""Architectural boundary tests — enforces import layering.

These are the dgov Lacustrine Pillars encoded as assertions:
- Kernel is pure (no I/O imports)
- Worker is isolated (no dgov imports)
- Settlement is pure (no orchestration imports)
"""

from __future__ import annotations

import ast
from pathlib import Path

import dgov  # noqa: F401 — primes sys.modules to avoid rogue root __init__.py

_SRC = Path(__file__).resolve().parent.parent / "src" / "dgov"


def _get_imports(filepath: Path) -> set[str]:
    """Extract all dgov.* import targets from a Python file."""
    tree = ast.parse(filepath.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("dgov"):
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dgov"):
                    imports.add(alias.name)
    return imports


class TestKernelPurity:
    """kernel.py must only import dgov.actions and dgov.types — no I/O."""

    def test_kernel_imports(self):
        imports = _get_imports(_SRC / "kernel.py")
        allowed = {"dgov.actions", "dgov.types"}
        violations = imports - allowed
        assert not violations, f"kernel.py imports forbidden modules: {violations}"


class TestWorkerIsolation:
    """worker.py is a subprocess — must only import its own support modules.

    `dgov.tool_policy` is allowed because it is a pure, dependency-free
    dataclass module shared between the worker subprocess and the main
    config loader. It imports nothing from the orchestration tier.
    """

    def test_worker_no_dgov_imports(self):
        imports = _get_imports(_SRC / "worker.py")
        allowed = {"dgov.workers.atomic", "dgov.worker", "dgov.tool_policy"}
        violations = imports - allowed
        assert not violations, f"worker.py imports forbidden modules: {violations}"


class TestSettlementPurity:
    """settlement.py must not import orchestration modules.

    `dgov.persistence` is allowed because the transient-scope check in
    `_check_transient_scope` needs to read worker_log events to catch
    unclaimed writes that the final git status no longer reflects. The
    import is scoped to the read path only (read_events), not writes.
    """

    def test_settlement_imports(self):
        imports = _get_imports(_SRC / "settlement.py")
        forbidden = {"dgov.runner", "dgov.kernel", "dgov.worker"}
        violations = imports & forbidden
        assert not violations, f"settlement.py imports forbidden modules: {violations}"


class TestWorkerLaunchBoundary:
    """Headless worker must run inside the installed dgov interpreter."""

    def test_headless_cmd_uses_current_interpreter(self):
        """Headless worker cmd starts with sys.executable, not uv run."""
        source = (_SRC / "workers" / "headless.py").read_text()
        assert "sys.executable" in source
        assert "_find_uv" not in source
