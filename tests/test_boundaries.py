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
    """worker.py is a subprocess — must not import any dgov modules."""

    def test_worker_no_dgov_imports(self):
        imports = _get_imports(_SRC / "worker.py")
        assert not imports, f"worker.py imports dgov modules: {imports}"


class TestSettlementPurity:
    """settlement.py must not import orchestration or persistence modules."""

    def test_settlement_imports(self):
        imports = _get_imports(_SRC / "settlement.py")
        forbidden = {"dgov.persistence", "dgov.runner", "dgov.kernel", "dgov.worker"}
        violations = imports & forbidden
        assert not violations, f"settlement.py imports forbidden modules: {violations}"
