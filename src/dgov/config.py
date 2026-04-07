"""Unified dgov configuration loader."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# -- Project config: per-repo conventions for workers --


_DEFAULT_AGENT = "accounts/fireworks/routers/kimi-k2p5-turbo"


@dataclass(frozen=True)
class ProjectConfig:
    """Per-project conventions. Loaded from .dgov/project.toml."""

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    source_extensions: tuple[str, ...] = (".py",)
    default_agent: str = _DEFAULT_AGENT
    # Worker SOP + settlement validate
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    # Settlement-specific (autofix + validate)
    lint_fix_cmd: str = "python -m ruff check --fix --unsafe-fixes {file}"
    format_check_cmd: str = "python -m ruff format --check {file}"
    test_markers: tuple[str, ...] = ()
    settlement_timeout: int = 120
    conventions: dict[str, str] = field(default_factory=dict)

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

    def to_prompt_section(self) -> str:
        """Render as text for injection into worker system prompt."""
        lines = [
            f"Language: {self.language}",
            f"Source: {self.src_dir}",
            f"Tests: {self.test_dir}",
            f"Test command: {self.resolve_test_cmd()}",
            f"Lint command: {self.resolve_lint_cmd()}",
            f"Format command: {self.resolve_format_cmd('<file>')}",
        ]
        if self.test_markers:
            lines.append(f"Test markers: {', '.join(self.test_markers)}")
        for key, val in self.conventions.items():
            lines.append(f"{key}: {val}")
        return "\n".join(lines)


def load_project_config(root: str | Path) -> ProjectConfig:
    """Load .dgov/project.toml from a project root. Returns defaults if missing."""
    path = Path(root) / ".dgov" / "project.toml"
    raw = _read_toml(path)
    if not raw:
        return ProjectConfig()

    proj = raw.get("project", {})
    conventions = raw.get("conventions", {})
    markers = proj.get("test_markers", ())
    if isinstance(markers, list):
        markers = tuple(markers)

    extensions = proj.get("source_extensions", (".py",))
    if isinstance(extensions, list):
        extensions = tuple(extensions)

    return ProjectConfig(
        language=proj.get("language", "python"),
        src_dir=proj.get("src_dir", "src/"),
        test_dir=proj.get("test_dir", "tests/"),
        source_extensions=extensions,
        default_agent=proj.get("default_agent", _DEFAULT_AGENT),
        test_cmd=proj.get("test_cmd", ProjectConfig.test_cmd),
        lint_cmd=proj.get("lint_cmd", ProjectConfig.lint_cmd),
        format_cmd=proj.get("format_cmd", ProjectConfig.format_cmd),
        lint_fix_cmd=proj.get("lint_fix_cmd", ProjectConfig.lint_fix_cmd),
        format_check_cmd=proj.get("format_check_cmd", ProjectConfig.format_check_cmd),
        test_markers=markers,
        settlement_timeout=proj.get("settlement_timeout", 120),
        conventions=conventions,
    )


def _read_toml(path: Path) -> dict:
    """Read a TOML file, return empty dict on any error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}
