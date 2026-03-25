"""Tests for dgov config loader."""

from __future__ import annotations

import pytest

from dgov.config import (
    _coerce_value,
    _deep_merge,
    _read_toml,
    get_provider_config,
    load_config,
    write_config,
)

pytestmark = pytest.mark.unit


def test_config_show_prints_toml(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from dgov.cli.admin import config_show

    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "fakehome")
    runner = CliRunner()
    result = runner.invoke(config_show, ["--project-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "plan_generation" in result.output
    assert "claude-cli" in result.output


def test_load_config_returns_defaults_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "fakehome")
    config = load_config(project_root=str(tmp_path / "noproject"))
    assert config["providers"]["plan_generation"]["transport"] == "claude-cli"
    assert config["defaults"]["agent"] == "qwen-35b"


def test_load_config_user_overrides_defaults(tmp_path, monkeypatch):
    user_dgov = tmp_path / ".dgov"
    user_dgov.mkdir()
    (user_dgov / "config.toml").write_text('[defaults]\nagent = "qwen-9b"\n')
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path)
    config = load_config(project_root=str(tmp_path / "noproject"))
    assert config["defaults"]["agent"] == "qwen-9b"
    # Other defaults still present
    assert config["defaults"]["timeout_s"] == 300


def test_load_config_project_overrides_user(tmp_path, monkeypatch):
    user_dgov = tmp_path / "home" / ".dgov"
    user_dgov.mkdir(parents=True)
    (user_dgov / "config.toml").write_text('[defaults]\nagent = "qwen-9b"\n')

    proj_dgov = tmp_path / "proj" / ".dgov"
    proj_dgov.mkdir(parents=True)
    (proj_dgov / "config.toml").write_text('[defaults]\nagent = "qwen-122b"\n')

    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "home")
    config = load_config(project_root=str(tmp_path / "proj"))
    assert config["defaults"]["agent"] == "qwen-122b"


def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"b": 99}, "e": 4}
    result = _deep_merge(base, override)
    assert result == {"a": {"b": 99, "c": 2}, "d": 3, "e": 4}


def test_get_provider_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path / "fakehome")
    cfg = get_provider_config("plan_generation", project_root=str(tmp_path))
    assert cfg["transport"] == "claude-cli"
    assert cfg["auth"] == "oauth"
    assert cfg["model"] == "claude-sonnet-4-6"


def test_write_config_creates_nested_key(tmp_path, monkeypatch):
    write_config("providers.review.model", "qwen/qwen3.5-35b", scope="user")

    # Verify it persists
    config = load_config(project_root=str(tmp_path / "noproject"))
    assert config["providers"]["review"]["model"] == "qwen/qwen3.5-35b"


def test_write_config_coerces_int(tmp_path, monkeypatch):
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path)
    write_config("defaults.timeout_s", "600", scope="user")
    data = _read_toml(tmp_path / ".dgov" / "config.toml")
    assert data["defaults"]["timeout_s"] == 600


def test_write_config_coerces_bool(tmp_path, monkeypatch):
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path)
    write_config("debug.verbose", "true", scope="user")
    data = _read_toml(tmp_path / ".dgov" / "config.toml")
    assert data["debug"]["verbose"] is True


def test_write_config_project_scope(tmp_path, monkeypatch):
    write_config("defaults.agent", "qwen-9b", scope="project", project_root=str(tmp_path))
    data = _read_toml(tmp_path / ".dgov" / "config.toml")
    assert data["defaults"]["agent"] == "qwen-9b"


def test_write_config_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setattr("dgov.config.Path.home", lambda: tmp_path)
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "config.toml").write_text('[defaults]\nagent = "qwen-35b"\n')

    write_config("defaults.timeout_s", "999", scope="user")
    data = _read_toml(dgov_dir / "config.toml")
    assert data["defaults"]["agent"] == "qwen-35b"
    assert data["defaults"]["timeout_s"] == 999


def test_coerce_value_string():
    assert _coerce_value("hello") == "hello"


def test_coerce_value_int():
    assert _coerce_value("42") == 42


def test_coerce_value_float():
    assert _coerce_value("3.14") == 3.14


def test_coerce_value_true():
    assert _coerce_value("true") is True


def test_coerce_value_false():
    assert _coerce_value("false") is False


def test_coerce_value_case_insensitive():
    assert _coerce_value("TRUE") is True
    assert _coerce_value("False") is False
