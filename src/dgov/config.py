"""Unified dgov configuration loader."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dgov.tool_policy import ToolPolicy, parse_tool_policy

# -- Project config: per-repo conventions for workers --


_DEFAULT_AGENT = "accounts/fireworks/routers/kimi-k2p5-turbo"
_DEFAULT_LLM_BASE_URL = "https://api.fireworks.ai/inference/v1"
_DEFAULT_LLM_API_KEY_ENV = "FIREWORKS_API_KEY"


@dataclass(frozen=True)
class ProjectConfig:
    """Per-project conventions. Loaded from .dgov/project.toml."""

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    source_extensions: tuple[str, ...] = (".py",)
    default_agent: str = _DEFAULT_AGENT
    llm_base_url: str = _DEFAULT_LLM_BASE_URL
    llm_api_key_env: str = _DEFAULT_LLM_API_KEY_ENV
    # Worker SOP + settlement validate
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    # Settlement-specific (autofix + validate)
    lint_fix_cmd: str = "python -m ruff check --fix --unsafe-fixes --show-fixes {file}"
    format_check_cmd: str = "python -m ruff format --check {file}"
    type_check_cmd: str = ""
    test_markers: tuple[str, ...] = ()
    worker_iteration_budget: int = 50
    worker_iteration_warn_at: int = 40
    worker_tree_max_lines: int = 80
    settlement_timeout: int = 120
    line_length: int = 99
    review_hooks: tuple[str, ...] = ()
    agents: dict[str, str] = field(default_factory=dict)
    conventions: dict[str, str] = field(default_factory=dict)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)

    def resolve_test_cmd(self, file: str = "") -> str:
        """Build the test command with substitutions."""
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
        return self.type_check_cmd

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
    markers = proj.get("test_markers", ())
    if isinstance(markers, list):
        markers = tuple(markers)

    hooks = proj.get("review_hooks", ())
    if isinstance(hooks, list):
        hooks = tuple(hooks)

    extensions = proj.get("source_extensions", (".py",))
    if isinstance(extensions, list):
        extensions = tuple(extensions)

    return ProjectConfig(
        language=proj.get("language", "python"),
        src_dir=proj.get("src_dir", "src/"),
        test_dir=proj.get("test_dir", "tests/"),
        source_extensions=extensions,
        default_agent=proj.get("default_agent", _DEFAULT_AGENT),
        llm_base_url=proj.get("llm_base_url", _DEFAULT_LLM_BASE_URL),
        llm_api_key_env=proj.get("llm_api_key_env", _DEFAULT_LLM_API_KEY_ENV),
        test_cmd=proj.get("test_cmd", ProjectConfig.test_cmd),
        lint_cmd=proj.get("lint_cmd", ProjectConfig.lint_cmd),
        format_cmd=proj.get("format_cmd", ProjectConfig.format_cmd),
        lint_fix_cmd=proj.get("lint_fix_cmd", ProjectConfig.lint_fix_cmd),
        format_check_cmd=proj.get("format_check_cmd", ProjectConfig.format_check_cmd),
        type_check_cmd=proj.get("type_check_cmd", ""),
        test_markers=markers,
        worker_iteration_budget=proj.get("worker_iteration_budget", 50),
        worker_iteration_warn_at=proj.get("worker_iteration_warn_at", 40),
        worker_tree_max_lines=proj.get("worker_tree_max_lines", 80),
        settlement_timeout=proj.get("settlement_timeout", 120),
        line_length=proj.get("line_length", 99),
        review_hooks=hooks,
        agents=agents,
        conventions=conventions,
        tool_policy=parse_tool_policy(raw.get("tool_policy", {})),
    )


def _read_toml(path: Path) -> dict:
    """Read a TOML file, return empty dict on any error."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}
