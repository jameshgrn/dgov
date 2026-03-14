"""Tests for governor auto-launch config resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.agents import get_governor_agent, write_project_config

pytestmark = pytest.mark.unit


class TestGetGovernorAgent:
    def test_from_project_config(self, tmp_path: Path) -> None:
        config = tmp_path / ".dgov" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[dgov]\ngovernor_agent = "claude"\ngovernor_permissions = "plan"\n')

        agent, perm = get_governor_agent(str(tmp_path))
        assert agent == "claude"
        assert perm == "plan"

    def test_from_global_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        global_cfg = tmp_path / "global" / ".dgov" / "config.toml"
        global_cfg.parent.mkdir(parents=True)
        global_cfg.write_text('[dgov]\ngovernor_agent = "codex"\n')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "global")

        # No project config — should fall through to global
        project = tmp_path / "repo"
        project.mkdir()
        agent, perm = get_governor_agent(str(project))
        assert agent == "codex"
        assert perm == ""

    def test_project_overrides_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_cfg = tmp_path / "global" / ".dgov" / "config.toml"
        global_cfg.parent.mkdir(parents=True)
        global_cfg.write_text('[dgov]\ngovernor_agent = "codex"\ngovernor_permissions = "plan"\n')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "global")

        project = tmp_path / "repo"
        project_cfg = project / ".dgov" / "config.toml"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(
            '[dgov]\ngovernor_agent = "gemini"\ngovernor_permissions = "bypassPermissions"\n'
        )

        agent, perm = get_governor_agent(str(project))
        assert agent == "gemini"
        assert perm == "bypassPermissions"

    def test_none_when_unconfigured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        project = tmp_path / "empty"
        project.mkdir()

        agent, perm = get_governor_agent(str(project))
        assert agent is None
        assert perm is None


class TestWriteProjectConfig:
    def test_first_time_writes_config(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        project.mkdir()

        write_project_config(str(project), "governor_agent", "claude")
        write_project_config(str(project), "governor_permissions", "plan")

        # Verify file is valid TOML and has both keys
        agent, perm = get_governor_agent(str(project))
        assert agent == "claude"
        assert perm == "plan"

    def test_preserves_existing_sections(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        config_path = project / ".dgov" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            '[openrouter]\napi_key = "sk-test"\n\n[dgov]\ndefault_agent = "pi"\n'
        )

        write_project_config(str(project), "governor_agent", "gemini")

        import tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        assert data["openrouter"]["api_key"] == "sk-test"
        assert data["dgov"]["governor_agent"] == "gemini"
        assert data["dgov"]["default_agent"] == "pi"
