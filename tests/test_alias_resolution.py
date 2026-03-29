"""Tests for alias_for resolution in load_routing_tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.agents import load_routing_tables

pytestmark = pytest.mark.unit


class TestAliasResolution:
    """Test alias_for resolution behavior in load_routing_tables."""

    def test_alias_for_resolves_to_target(self, tmp_path, monkeypatch):
        """alias_for entry should resolve to target's backends."""
        # Create user-global config
        global_home = tmp_path / "global"
        global_dgov = global_home / ".dgov"
        global_dgov.mkdir(parents=True)
        global_file = global_dgov / "agents.toml"
        global_file.write_text("""
[routing.generate-t1]
backends = ["backend-a", "backend-b"]

[routing.worker]
alias_for = "generate-t1"
""")

        # Mock Path.home to use our test config
        monkeypatch.setattr(Path, "home", lambda: global_home)

        result = load_routing_tables(project_root=None)

        assert "worker" in result
        assert result["worker"] == ["backend-a", "backend-b"]
        assert "generate-t1" in result
        assert result["generate-t1"] == ["backend-a", "backend-b"]

    def test_alias_for_missing_target(self, tmp_path, monkeypatch, caplog):
        """alias_for pointing to non-existent target should be skipped with warning."""
        global_home = tmp_path / "global"
        global_dgov = global_home / ".dgov"
        global_dgov.mkdir(parents=True)
        global_file = global_dgov / "agents.toml"
        global_file.write_text("""
[routing.worker]
alias_for = "nonexistent"
""")

        monkeypatch.setattr(Path, "home", lambda: global_home)

        with caplog.at_level("WARNING"):
            result = load_routing_tables(project_root=None)

        # Alias should not be in result
        assert "worker" not in result

        # Warning should be logged
        assert "unresolved" in caplog.text.lower() or "not found" in caplog.text.lower()

    def test_alias_for_multi_level_resolution(self, tmp_path, monkeypatch):
        """alias_for can resolve through other aliases when processed in order."""
        # Note: The current implementation allows multi-level resolution
        # because b gets resolved and added to routing_dict before c is processed
        global_home = tmp_path / "global"
        global_dgov = global_home / ".dgov"
        global_dgov.mkdir(parents=True)
        global_file = global_dgov / "agents.toml"
        global_file.write_text("""
[routing.a]
backends = ["x"]

[routing.b]
alias_for = "a"

[routing.c]
alias_for = "b"
""")

        monkeypatch.setattr(Path, "home", lambda: global_home)

        result = load_routing_tables(project_root=None)

        # b should resolve to a's backends
        assert "b" in result
        assert result["b"] == ["x"]

        # c resolves through b to a's backends (due to iteration order)
        assert "c" in result
        assert result["c"] == ["x"]

    def test_alias_for_with_backends_ignores_alias(self, tmp_path, monkeypatch):
        """If both alias_for and backends present, backends takes precedence."""
        global_home = tmp_path / "global"
        global_dgov = global_home / ".dgov"
        global_dgov.mkdir(parents=True)
        global_file = global_dgov / "agents.toml"
        global_file.write_text("""
[routing.generate-t1]
backends = ["backend-a", "backend-b"]

[routing.worker]
backends = ["direct"]
alias_for = "generate-t1"
""")

        monkeypatch.setattr(Path, "home", lambda: global_home)

        result = load_routing_tables(project_root=None)

        # backends takes precedence over alias_for
        assert "worker" in result
        assert result["worker"] == ["direct"]

    def test_alias_for_project_local_override(self, tmp_path, monkeypatch):
        """Project-local alias should work even when targeting global route."""
        global_home = tmp_path / "global"
        global_dgov = global_home / ".dgov"
        global_dgov.mkdir(parents=True)
        global_file = global_dgov / "agents.toml"
        global_file.write_text("""
[routing.generate-t1]
backends = ["backend-a", "backend-b"]
""")

        # Create project with an alias to global route
        project_root = tmp_path / "project"
        project_root.mkdir()
        local_dgov = project_root / ".dgov"
        local_dgov.mkdir(parents=True)
        local_file = local_dgov / "agents.toml"
        local_file.write_text("""
[routing.worker]
alias_for = "generate-t1"
""")

        monkeypatch.setattr(Path, "home", lambda: global_home)

        result = load_routing_tables(project_root=str(project_root))

        # Project alias should resolve to global target
        assert "worker" in result
        assert result["worker"] == ["backend-a", "backend-b"]
        assert "generate-t1" in result
