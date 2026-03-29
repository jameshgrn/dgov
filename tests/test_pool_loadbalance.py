"""Tests for least-loaded backend selection in the router."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_least_loaded_picks_emptiest():
    """Mock 3 backends (b0=2 active, b1=0, b2=1). Assert resolve_agent returns b1."""
    from dgov.router import resolve_agent

    # Mock routing tables: pool -> [b0, b1, b2]
    with patch("dgov.router._load_routing_tables") as mock_tables:
        mock_tables.return_value = {"pool": ["b0", "b1", "b2"]}

        # Mock registry entries for each backend
        def mock_agent_def():
            m = MagicMock()
            m.health = MagicMock()
            m.health.check = None
            m.max_concurrent = None
            m.groups = ()
            return m

        with patch("dgov.agents.load_registry") as mock_registry:
            mock_registry.return_value = {
                "b0": mock_agent_def(),
                "b1": mock_agent_def(),
                "b2": mock_agent_def(),
            }

            with patch("dgov.agents.load_groups") as mock_groups:
                mock_groups.return_value = {}

                # Mock active worker counts: b0=2, b1=0, b2=1
                with patch("dgov.status._count_active_agent_workers") as mock_count:
                    mock_count.side_effect = lambda _root, agent: {
                        "b0": 2,
                        "b1": 0,
                        "b2": 1,
                    }.get(agent, 0)

                    with patch("dgov.backend.get_backend") as mock_backend:
                        mock_backend.return_value.bulk_info.return_value = {}

                        with patch("dgov.persistence.all_panes") as mock_panes:
                            mock_panes.return_value = []

                            result = resolve_agent("pool", "/tmp", "/tmp")
                            assert result == ("b1", "pool")


@pytest.mark.unit
def test_tiebreaker_preserves_config_order():
    """Mock 2 backends both with 0 active. First in config wins."""
    from dgov.router import resolve_agent

    with patch("dgov.router._load_routing_tables") as mock_tables:
        mock_tables.return_value = {"pool": ["b0", "b1"]}

        def mock_agent_def():
            m = MagicMock()
            m.health = MagicMock()
            m.health.check = None
            m.max_concurrent = None
            m.groups = ()
            return m

        with patch("dgov.agents.load_registry") as mock_registry:
            mock_registry.return_value = {
                "b0": mock_agent_def(),
                "b1": mock_agent_def(),
            }

            with patch("dgov.agents.load_groups") as mock_groups:
                mock_groups.return_value = {}

                # Both have 0 active workers - tie should break to first in config
                with patch("dgov.status._count_active_agent_workers") as mock_count:
                    mock_count.side_effect = lambda _root, agent: {"b0": 0, "b1": 0}.get(agent, 0)

                    with patch("dgov.backend.get_backend") as mock_backend:
                        mock_backend.return_value.bulk_info.return_value = {}

                        with patch("dgov.persistence.all_panes") as mock_panes:
                            mock_panes.return_value = []

                            result = resolve_agent("pool", "/tmp", "/tmp")
                            assert result == ("b0", "pool")


@pytest.mark.unit
def test_single_viable_backend():
    """2 fail health, 1 viable. Returns the viable one."""
    from dgov.router import resolve_agent

    with patch("dgov.router._load_routing_tables") as mock_tables:
        mock_tables.return_value = {"pool": ["b0", "b1", "b2"]}

        def mock_agent_def(health_check=None):
            m = MagicMock()
            m.health = MagicMock()
            m.health.check = health_check
            m.max_concurrent = None
            m.groups = ()
            return m

        with patch("dgov.agents.load_registry") as mock_registry:
            mock_registry.return_value = {
                "b0": mock_agent_def(health_check="false"),  # Fails (exit 1)
                "b1": mock_agent_def(health_check="false"),  # Fails (exit 1)
                "b2": mock_agent_def(health_check="true"),  # Passes (exit 0)
            }

            with patch("dgov.agents.load_groups") as mock_groups:
                mock_groups.return_value = {}

                # Mock subprocess.run to simulate health check results
                with patch("dgov.router.subprocess.run") as mock_run:

                    def mock_run_side_effect(cmd, **kwargs):
                        result = MagicMock()
                        if "false" in cmd:
                            result.returncode = 1
                        else:
                            result.returncode = 0
                        return result

                    mock_run.side_effect = mock_run_side_effect

                    with patch("dgov.status._count_active_agent_workers") as mock_count:
                        mock_count.return_value = 0  # All have 0 active workers

                        with patch("dgov.backend.get_backend") as mock_backend:
                            mock_backend.return_value.bulk_info.return_value = {}

                            with patch("dgov.persistence.all_panes") as mock_panes:
                                mock_panes.return_value = []

                                result = resolve_agent("pool", "/tmp", "/tmp")
                                assert result == ("b2", "pool")
