"""Tests for agent routing and role resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.agents import load_routing_tables

pytestmark = pytest.mark.unit


class TestRoutingTablePrecedence:
    """Test that project-local routing takes precedence over user-global."""

    def test_project_local_overrides_user_global(self, tmp_path):
        """Project-local agents.toml should override user-global for same logical names."""
        # Create user-global config
        global_config = tmp_path / "global" / ".dgov"
        global_config.mkdir(parents=True)
        global_file = global_config / "agents.toml"
        global_file.write_text("""
[routing.test-route]
backends = ["global-backend-1", "global-backend-2"]
""")

        # Create project-local config with same route name but different backends
        project_root = tmp_path / "project"
        project_root.mkdir()
        local_config = project_root / ".dgov"
        local_config.mkdir()
        local_file = local_config / "agents.toml"
        local_file.write_text("""
[routing.test-route]
backends = ["local-backend"]
""")

        # Without project_root, should get global (mock Path.home to use our test config)
        with patch.object(Path, "home", return_value=tmp_path / "global"):
            tables_global = load_routing_tables(project_root=None)
            assert tables_global.get("test-route") == ["global-backend-1", "global-backend-2"]

        # With project_root, local should override
        with patch.object(Path, "home", return_value=tmp_path / "global"):
            tables_local = load_routing_tables(project_root=str(project_root))
            assert tables_local.get("test-route") == ["local-backend"]

    def test_abstract_role_routes_exist(self, tmp_path):
        """Ensure worker, supervisor, manager, lt-gov abstract roles are routable."""
        # Create a minimal project-local config
        project_root = tmp_path / "project"
        project_root.mkdir()
        local_config = project_root / ".dgov"
        local_config.mkdir()
        local_file = local_config / "agents.toml"
        local_file.write_text("""
[routing.worker]
backends = ["worker-1", "worker-2"]

[routing.supervisor]
backends = ["supervisor-1"]

[routing.manager]
backends = ["manager-1"]

[routing.lt-gov]
backends = ["ltgov-1", "ltgov-2"]
""")

        tables = load_routing_tables(project_root=str(project_root))

        assert "worker" in tables
        assert "supervisor" in tables
        assert "manager" in tables
        assert "lt-gov" in tables
        assert tables["worker"] == ["worker-1", "worker-2"]
