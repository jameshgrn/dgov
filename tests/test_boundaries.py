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
        imports.update(_dgov_imports_from_node(node))
    return imports


def _dgov_imports_from_node(node: ast.AST) -> set[str]:
    if isinstance(node, ast.ImportFrom):
        return _dgov_from_import_from(node)
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names if alias.name.startswith("dgov")}
    return set()


def _dgov_from_import_from(node: ast.ImportFrom) -> set[str]:
    if node.module is None or not node.module.startswith("dgov"):
        return set()
    return {node.module}


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

    `dgov.workers.provider` is allowed because it owns provider calls and
    retry policy behind a narrow worker-support object.

    `dgov.workers.runtime` is allowed because it owns subprocess runtime
    helpers shared by worker, planner, and researcher roles.

    `dgov.workers.config` is allowed because it owns worker-facing config and
    payload translation without importing orchestration modules.
    """

    def test_worker_no_dgov_imports(self):
        imports = _get_imports(_SRC / "worker.py")
        allowed = {
            "dgov.workers.atomic",
            "dgov.workers.config",
            "dgov.workers.provider",
            "dgov.workers.runtime",
            "dgov.tool_policy",
        }
        violations = imports - allowed
        assert not violations, f"worker.py imports forbidden modules: {violations}"

    def test_worker_provider_is_leaf_module(self):
        imports = _get_imports(_SRC / "workers" / "provider.py")
        assert not imports, f"dgov.workers.provider imports forbidden modules: {imports}"

    def test_worker_config_imports_only_pure_policy(self):
        imports = _get_imports(_SRC / "workers" / "config.py")
        allowed = {"dgov.tool_policy"}
        violations = imports - allowed
        assert not violations, f"dgov.workers.config imports forbidden modules: {violations}"

    def test_worker_runtime_imports_only_worker_support(self):
        imports = _get_imports(_SRC / "workers" / "runtime.py")
        allowed = {"dgov.workers.atomic", "dgov.workers.config"}
        violations = imports - allowed
        assert not violations, f"dgov.workers.runtime imports forbidden modules: {violations}"

    def test_project_config_does_not_import_worker_tools(self):
        imports = _get_imports(_SRC / "config.py")
        assert "dgov.workers.atomic" not in imports

    def test_worker_tools_module_does_not_export_config(self):
        import dgov.workers.atomic as atomic

        assert "AtomicConfig" not in vars(atomic)


class TestAgentRoleBoundaries:
    """Planner and researcher may share worker-support modules, not worker.py."""

    def test_planner_does_not_import_worker_script(self):
        imports = _get_imports(_SRC / "planner.py")
        assert "dgov.worker" not in imports

    def test_researcher_does_not_import_worker_script(self):
        imports = _get_imports(_SRC / "researcher.py")
        assert "dgov.worker" not in imports


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


class TestSettlementFlowBoundary:
    """settlement_flow.py must not reach back into orchestration internals."""

    def test_settlement_flow_imports(self):
        imports = _get_imports(_SRC / "settlement_flow.py")
        forbidden = {"dgov.runner", "dgov.kernel", "dgov.worker"}
        violations = imports & forbidden
        assert not violations, f"settlement_flow.py imports forbidden modules: {violations}"

    def test_settlement_flow_uses_public_semantic_api(self):
        source = (_SRC / "settlement_flow.py").read_text()
        tree = ast.parse(source)
        private_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dgov.semantic_settlement":
                private_names.update(
                    alias.name for alias in node.names if alias.name.startswith("_")
                )
        assert not private_names, (
            f"settlement_flow.py imports private semantic API: {private_names}"
        )
        assert "from dgov.semantic_settlement import _" not in source


class TestPromptBuilderBoundary:
    """prompt_builder.py must not depend on settlement gate orchestration."""

    def test_prompt_builder_does_not_import_settlement(self):
        imports = _get_imports(_SRC / "prompt_builder.py")
        assert "dgov.settlement" not in imports


class TestCliCompositionBoundary:
    """CLI command modules may compose through public sibling APIs only."""

    def test_cli_modules_do_not_import_private_sibling_helpers(self):
        offenders: dict[str, set[str]] = {}
        for rel_path in ("cli/fix.py", "cli/plan_create.py", "cli/run.py", "cli/sentrux.py"):
            path = _SRC / rel_path
            tree = ast.parse(path.read_text())
            private_names: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module not in {"dgov.cli.compile", "dgov.cli.run"}:
                    continue
                private_names.update(
                    alias.name for alias in node.names if alias.name.startswith("_")
                )
            if private_names:
                offenders[rel_path] = private_names
        assert not offenders, f"CLI modules import private sibling helpers: {offenders}"


class TestWorkerLaunchBoundary:
    """Headless worker must run inside the installed dgov interpreter."""

    def test_headless_cmd_uses_current_interpreter(self):
        """Headless worker cmd starts with sys.executable, not uv run."""
        source = (_SRC / "workers" / "headless.py").read_text()
        assert "sys.executable" in source
        assert "_find_uv" not in source
