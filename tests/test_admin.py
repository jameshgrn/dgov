"""Tests for dgov admin commands."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_doctor_auth_warns_on_api_key_with_oauth(tmp_path, monkeypatch):
    """Doctor flags ANTHROPIC_API_KEY when config says auth=oauth."""
    from click.testing import CliRunner

    from dgov.cli.admin import doctor_cmd

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key")
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "fakehome")

    runner = CliRunner()
    result = runner.invoke(doctor_cmd, ["--project-root", str(tmp_path)])
    assert "ANTHROPIC_API_KEY" in result.output


def test_doctor_auth_passes_without_api_key(tmp_path, monkeypatch):
    """Doctor passes auth check when no conflicting env var."""
    from click.testing import CliRunner

    from dgov.cli.admin import doctor_cmd

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "fakehome")

    runner = CliRunner()
    result = runner.invoke(doctor_cmd, ["--project-root", str(tmp_path)])
    assert "overrides OAuth" not in result.output
