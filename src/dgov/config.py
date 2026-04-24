"""Unified dgov configuration loader."""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dgov.tool_policy import parse_tool_policy
from dgov.workers.atomic import (
    AtomicConfig,
    atomic_config_from_payload,
    atomic_config_to_payload,
)

# -- Project config: per-repo conventions for workers --


_DEFAULT_AGENT = "accounts/fireworks/routers/kimi-k2p5-turbo"
_DEFAULT_SCOPE_IGNORE_FILES = (".venv", "uv.lock", "__pycache__", "*.pyc")


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
    scope_ignore_files: tuple[str, ...] = _DEFAULT_SCOPE_IGNORE_FILES
    setup_cmd: str | None = None
    departments: dict[str, list[str]] = field(default_factory=dict)

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

    def llm_runtime_settings(self) -> tuple[str, str]:
        """Return the configured OpenAI-compatible runtime endpoint settings."""
        return self.llm_base_url, self.llm_api_key_env

    def to_worker_payload(self) -> dict[str, object]:
        """Serialize the worker-facing config fields for subprocess dispatch."""
        return atomic_config_to_payload(self.to_atomic_config())

    def to_atomic_config(self) -> AtomicConfig:
        """Project-facing subset needed by the worker. Drops governor-only fields."""
        atomic_values = {f.name: getattr(self, f.name) for f in fields(AtomicConfig)}
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
        return cls(**atomic_values)

    def to_prompt_section(self) -> str:
        """Render as text for injection into worker system prompt."""
        lines = [
            f"Language: {self.language}",
            f"Source: {self.src_dir}",
            f"Tests: {self.test_dir}",
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


def load_project_config(root: str | Path) -> ProjectConfig:
    """Load .dgov/project.toml from a project root. Returns defaults if missing."""
    path = Path(root) / ".dgov" / "project.toml"
    raw = _read_toml(path)
    if not raw:
        return ProjectConfig()

    proj = raw.get("project", {})
    agents = raw.get("agents", {})
    conventions = raw.get("conventions", {})
    departments = raw.get("departments", {})
    markers = proj.get("test_markers", ())
    if isinstance(markers, list):
        markers = tuple(markers)

    hooks = proj.get("review_hooks", ())
    if isinstance(hooks, list):
        hooks = tuple(hooks)

    extensions = proj.get("source_extensions", (".py",))
    if isinstance(extensions, list):
        extensions = tuple(extensions)

    scope_section = raw.get("scope", {})
    ignore_raw = scope_section.get("ignore_files", ()) if isinstance(scope_section, dict) else ()
    if isinstance(ignore_raw, list):
        configured_scope_ignores = tuple(str(p).strip() for p in ignore_raw if str(p).strip())
    else:
        configured_scope_ignores = ()
    # Reserved-path guard: the scope ignore list must not shadow governor-owned
    # files, otherwise a worker could silently mutate them.
    _RESERVED_PATHS = frozenset({".sentrux/baseline.json"})
    bad = sorted(set(configured_scope_ignores) & _RESERVED_PATHS)
    if bad:
        raise ValueError(f"project.toml [scope] ignore_files cannot include reserved paths: {bad}")
    scope_ignore_files = tuple(
        dict.fromkeys((*ProjectConfig.scope_ignore_files, *configured_scope_ignores))
    )

    return ProjectConfig(
        language=proj.get("language", ProjectConfig.language),
        src_dir=proj.get("src_dir", ProjectConfig.src_dir),
        test_dir=proj.get("test_dir", ProjectConfig.test_dir),
        source_extensions=extensions,
        default_agent=proj.get("default_agent", _DEFAULT_AGENT),
        llm_base_url=proj.get("llm_base_url", ProjectConfig.llm_base_url),
        llm_api_key_env=proj.get("llm_api_key_env", ProjectConfig.llm_api_key_env),
        test_cmd=proj.get("test_cmd", ProjectConfig.test_cmd),
        lint_cmd=proj.get("lint_cmd", ProjectConfig.lint_cmd),
        format_cmd=proj.get("format_cmd", ProjectConfig.format_cmd),
        lint_fix_cmd=proj.get("lint_fix_cmd", ProjectConfig.lint_fix_cmd),
        format_check_cmd=proj.get("format_check_cmd", ProjectConfig.format_check_cmd),
        type_check_cmd=proj.get("type_check_cmd") or None,
        test_markers=markers,
        worker_iteration_budget=proj.get("worker_iteration_budget", 50),
        worker_iteration_warn_at=proj.get("worker_iteration_warn_at", 40),
        worker_tree_max_lines=proj.get("worker_tree_max_lines", 80),
        bootstrap_timeout=proj.get("bootstrap_timeout", 300),
        settlement_timeout=proj.get("settlement_timeout", 120),
        line_length=proj.get("line_length", 99),
        review_hooks=hooks,
        agents=agents,
        conventions=conventions,
        tool_policy=parse_tool_policy(raw.get("tool_policy", {})),
        scope_ignore_files=scope_ignore_files,
        setup_cmd=proj.get("setup_cmd") or None,
        departments=departments,
    )


def _read_toml(path: Path) -> dict:
    """Read a TOML file, return empty dict on any error."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}
