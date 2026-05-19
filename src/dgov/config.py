"""Unified dgov configuration loader."""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dgov.tool_policy import parse_tool_policy
from dgov.verify import VerifyRecipe, load_verify_recipes
from dgov.workers.config import (
    AtomicConfig,
    ProviderConfig,
    atomic_config_from_payload,
    atomic_config_to_payload,
    provider_configs_from_project_toml,
    selected_provider_from_project_toml,
)

# -- Project config: per-repo conventions for workers --


_DEFAULT_AGENT = ""
_DEFAULT_SCOPE_IGNORE_FILES = (".venv", "uv.lock", "__pycache__", "*.pyc")
_RESERVED_SCOPE_IGNORE_PATHS = (
    ".sentrux/baseline.json",
    ".sentrux/dgov-baseline.json",
    ".coverage-baseline/",
)


@dataclass(frozen=True)
class ProjectConfig(AtomicConfig):
    """Per-project conventions. Loaded from .dgov/project.toml.

    Inherits every worker-facing field from AtomicConfig; adds governor-only
    fields (settlement, review, agent routing). Never duplicate a worker field
    here — put it on AtomicConfig so the worker subprocess sees it too.
    """

    # Governor-only fields below. The worker subprocess never consumes these.
    source_extensions: tuple[str, ...] = (".py",)
    default_agent: str = _DEFAULT_AGENT
    format_check_cmd: str = "python -m ruff format --check {file}"
    bootstrap_timeout: int = 300
    settlement_timeout: int = 120
    review_hooks: tuple[str, ...] = ()
    agents: dict[str, str] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    scope_allow_files: tuple[str, ...] = ()
    scope_deny_files: tuple[str, ...] = ()
    scope_ignore_files: tuple[str, ...] = _DEFAULT_SCOPE_IGNORE_FILES
    setup_cmd: str | None = None
    coverage_cmd: str | None = None
    coverage_threshold: float = 2.0
    sentrux_mode: str = "diff"
    sentrux_stale_commits: int = 10
    sentrux_stale_days: int = 14
    departments: dict[str, list[str]] = field(default_factory=dict)
    verify_recipes: dict[str, VerifyRecipe] = field(default_factory=dict)

    def resolve_test_cmd(self, file: str = "") -> str:
        """Build the test command with substitutions."""
        if not self.test_cmd:
            return ""
        cmd = self.test_cmd.replace("{test_dir}", self.test_dir)
        if file:
            cmd = cmd.replace("{test_dir}", file).replace(self.test_dir, file)
        return cmd

    def resolve_lint_cmd(self, file: str = "") -> str:
        target = file if file else self.src_dir
        return self.lint_cmd.replace("{file}", target)

    def resolve_format_cmd(self, file: str) -> str:
        return self.format_cmd.replace("{file}", file)

    def resolve_format_check_cmd(self, file: str) -> str:
        return self.format_check_cmd.replace("{file}", file)

    def resolve_lint_fix_cmd(self, file: str = "") -> str:
        target = file if file else self.src_dir
        return self.lint_fix_cmd.replace("{file}", target)

    def resolve_type_check_cmd(self) -> str:
        return self.type_check_cmd or ""

    def get_department_for_path(self, path: str) -> str | None:
        """Return the department name for a given path using fnmatch patterns."""
        for dept, patterns in self.departments.items():
            for pattern in patterns:
                if fnmatch.fnmatch(path, pattern):
                    return dept
        return None

    def provider_config(self, provider: str | None = None) -> ProviderConfig:
        """Return provider endpoint config by name, or the active provider."""
        name = (provider or self.llm_provider).strip()
        if not name:
            raise ValueError(
                ".dgov/project.toml must set [project].provider, or the task must set provider"
            )
        if name in self.providers:
            return self.providers[name]
        raise ValueError(
            f".dgov/project.toml selects provider {name!r}, but [providers.{name}] is not defined"
        )

    def provider_default_agents(self) -> dict[str, str]:
        """Return provider-specific default model/router names."""
        return {
            name: provider.default_agent
            for name, provider in self.providers.items()
            if provider.default_agent
        }

    def llm_runtime_settings(self, provider: str | None = None) -> tuple[str, str]:
        """Return the configured OpenAI-compatible runtime endpoint settings."""
        endpoint = self.provider_config(provider)
        return endpoint.base_url, endpoint.api_key_env

    def to_worker_payload(self, provider: str | None = None) -> dict[str, object]:
        """Serialize the worker-facing config fields for subprocess dispatch."""
        return atomic_config_to_payload(self.to_atomic_config(provider))

    def to_atomic_config(self, provider: str | None = None) -> AtomicConfig:
        """Project-facing subset needed by the worker. Drops governor-only fields."""
        endpoint = self.provider_config(provider)
        atomic_values = {f.name: getattr(self, f.name) for f in fields(AtomicConfig)}
        atomic_values.update(
            llm_provider=endpoint.name,
            llm_base_url=endpoint.base_url,
            llm_api_key_env=endpoint.api_key_env,
        )
        return AtomicConfig(**atomic_values)

    @classmethod
    def from_worker_payload(cls, raw: Mapping[str, Any]) -> ProjectConfig:
        """Round-trip a worker payload back into ProjectConfig.

        Governor-only fields are absent from the payload, so they fall back to
        their class defaults. This mirror is used by tests that verify
        worker-bound fields survive serialization.
        """
        atomic = atomic_config_from_payload(raw)
        atomic_values = {f.name: getattr(atomic, f.name) for f in fields(AtomicConfig)}
        providers = {}
        if atomic.llm_provider:
            providers[atomic.llm_provider] = ProviderConfig(
                name=atomic.llm_provider,
                base_url=atomic.llm_base_url,
                api_key_env=atomic.llm_api_key_env,
            )
        return cls(**atomic_values, providers=providers)

    def to_prompt_section(self) -> str:
        """Render as text for injection into worker system prompt."""
        lines = [
            f"Language: {self.language}",
            f"Source: {self.src_dir}",
            f"Tests: {self.test_dir}",
            f"LLM provider: {self.llm_provider}",
            f"LLM base URL: {self.llm_base_url}",
            f"LLM API key env: {self.llm_api_key_env}",
            f"Test command: {self.resolve_test_cmd()}",
            f"Lint command: {self.resolve_lint_cmd()}",
            f"Format command: {self.resolve_format_cmd('<file>')}",
        ]
        if self.type_check_cmd:
            lines.append(f"Type check command: {self.type_check_cmd}")
        if self.test_markers:
            lines.append(f"Test markers: {', '.join(self.test_markers)}")
        lines.append(f"Worker iteration budget: {self.worker_iteration_budget}")
        lines.append(f"Worker iteration warn at: {self.worker_iteration_warn_at}")
        lines.append(f"Worker tree max lines: {self.worker_tree_max_lines}")
        for key, val in self.conventions.items():
            lines.append(f"{key}: {val}")
        for line in self.tool_policy.to_prompt_lines():
            lines.append(f"Tool policy: {line}")
        return "\n".join(lines)


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in raw:
        return {}
    value = raw[key]
    if not isinstance(value, dict):
        raise ValueError(f".dgov/project.toml [{key}] must be a table")
    return value


def _tuple_if_list(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _configured_scope_ignores(raw: Mapping[str, Any]) -> tuple[str, ...]:
    scope_section = _table(raw, "scope")
    if "ignore_files" not in scope_section:
        return ()
    ignore_raw = scope_section["ignore_files"]
    if not isinstance(ignore_raw, list):
        raise ValueError(".dgov/project.toml [scope].ignore_files must be a list of strings")
    if not all(isinstance(pattern, str) for pattern in ignore_raw):
        raise ValueError(".dgov/project.toml [scope].ignore_files must be a list of strings")
    return tuple(pattern.strip() for pattern in ignore_raw if pattern.strip())


def _scope_patterns(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    scope_section = _table(raw, "scope")
    if key not in scope_section:
        return ()
    patterns_raw = scope_section[key]
    if not isinstance(patterns_raw, list):
        raise ValueError(f".dgov/project.toml [scope].{key} must be a list of strings")
    if not all(isinstance(pattern, str) for pattern in patterns_raw):
        raise ValueError(f".dgov/project.toml [scope].{key} must be a list of strings")
    return tuple(pattern.strip() for pattern in patterns_raw if pattern.strip())


def _validate_scope_ignores(configured_scope_ignores: tuple[str, ...]) -> None:
    bad = sorted(
        path
        for path in configured_scope_ignores
        if any(
            path == reserved or path.startswith(reserved)
            for reserved in _RESERVED_SCOPE_IGNORE_PATHS
        )
    )
    if bad:
        raise ValueError(f"project.toml [scope] ignore_files cannot include reserved paths: {bad}")


def _scope_ignore_files(raw: Mapping[str, Any]) -> tuple[str, ...]:
    configured_scope_ignores = _configured_scope_ignores(raw)
    # Reserved-path guard: the scope ignore list must not shadow governor-owned
    # files, otherwise a worker could silently mutate them.
    _validate_scope_ignores(configured_scope_ignores)
    return tuple(dict.fromkeys((*ProjectConfig.scope_ignore_files, *configured_scope_ignores)))


def _string_table(raw: Mapping[str, Any], key: str) -> dict[str, str]:
    table = _table(raw, key)
    bad = sorted(name for name, value in table.items() if not isinstance(value, str))
    if bad:
        raise ValueError(f".dgov/project.toml [{key}] values must be strings: {', '.join(bad)}")
    return {str(name): value for name, value in table.items()}


def _sentrux_mode(raw: Mapping[str, Any]) -> str:
    sentrux = _table(raw, "sentrux")
    mode = str(sentrux.get("mode", ProjectConfig.sentrux_mode)).strip().lower()
    if mode not in {"diff", "strict"}:
        raise ValueError(".dgov/project.toml [sentrux].mode must be 'diff' or 'strict'")
    return mode


def _sentrux_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    sentrux = _table(raw, "sentrux")
    value = sentrux.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f".dgov/project.toml [sentrux].{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f".dgov/project.toml [sentrux].{key} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f".dgov/project.toml [sentrux].{key} must be >= 0")
    return parsed


def _optional_project_string(proj: Mapping[str, Any], key: str) -> str:
    raw = proj.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f".dgov/project.toml [project].{key} must be a string")
    return raw.strip()


def _worker_config_fields(raw: Mapping[str, Any], proj: Mapping[str, Any]) -> dict[str, Any]:
    provider = selected_provider_from_project_toml(raw)
    return {
        "language": proj.get("language", ProjectConfig.language),
        "src_dir": proj.get("src_dir", ProjectConfig.src_dir),
        "test_dir": proj.get("test_dir", ProjectConfig.test_dir),
        "llm_provider": provider.name,
        "llm_base_url": provider.base_url,
        "llm_api_key_env": provider.api_key_env,
        "test_cmd": proj.get("test_cmd", ProjectConfig.test_cmd),
        "lint_cmd": proj.get("lint_cmd", ProjectConfig.lint_cmd),
        "format_cmd": proj.get("format_cmd", ProjectConfig.format_cmd),
        "lint_fix_cmd": proj.get("lint_fix_cmd", ProjectConfig.lint_fix_cmd),
        "type_check_cmd": proj.get("type_check_cmd") or None,
        "test_markers": _tuple_if_list(proj.get("test_markers", ())),
        "worker_iteration_budget": proj.get("worker_iteration_budget", 50),
        "worker_iteration_warn_at": proj.get("worker_iteration_warn_at", 40),
        "worker_tree_max_lines": proj.get("worker_tree_max_lines", 80),
        "line_length": proj.get("line_length", 99),
    }


def _governor_config_fields(raw: Mapping[str, Any], proj: Mapping[str, Any]) -> dict[str, Any]:
    provider = selected_provider_from_project_toml(raw)
    default_agent = _optional_project_string(proj, "default_agent")
    default_agent = default_agent or provider.default_agent or _DEFAULT_AGENT
    return {
        "source_extensions": _tuple_if_list(proj.get("source_extensions", (".py",))),
        "default_agent": default_agent,
        "format_check_cmd": proj.get("format_check_cmd", ProjectConfig.format_check_cmd),
        "bootstrap_timeout": proj.get("bootstrap_timeout", 300),
        "settlement_timeout": proj.get("settlement_timeout", 120),
        "review_hooks": _tuple_if_list(proj.get("review_hooks", ())),
        "agents": _string_table(raw, "agents"),
        "providers": provider_configs_from_project_toml(raw),
        "conventions": _table(raw, "conventions"),
        "tool_policy": parse_tool_policy(_table(raw, "tool_policy")),
        "scope_allow_files": _scope_patterns(raw, "allow_files"),
        "scope_deny_files": _scope_patterns(raw, "deny_files"),
        "scope_ignore_files": _scope_ignore_files(raw),
        "setup_cmd": proj.get("setup_cmd") or None,
        "coverage_cmd": proj.get("coverage_cmd") or None,
        "coverage_threshold": proj.get("coverage_threshold", 2.0),
        "sentrux_mode": _sentrux_mode(raw),
        "sentrux_stale_commits": _sentrux_int(raw, "stale_commits", 10),
        "sentrux_stale_days": _sentrux_int(raw, "stale_days", 14),
        "departments": _table(raw, "departments"),
        "verify_recipes": load_verify_recipes(raw),
    }


def _project_config_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    proj = _table(raw, "project")
    return {
        **_worker_config_fields(raw, proj),
        **_governor_config_fields(raw, proj),
    }


def load_project_config(root: str | Path) -> ProjectConfig:
    """Load .dgov/project.toml from a project root. Returns defaults if missing."""
    path = Path(root) / ".dgov" / "project.toml"
    raw = _read_toml(path)
    if not raw:
        return ProjectConfig()

    return ProjectConfig(**_project_config_fields(raw))


def _read_toml(path: Path) -> dict:
    """Read a TOML file, returning defaults only when the file is absent."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
