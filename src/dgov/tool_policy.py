"""Typed worker tool policy shared by config loading and worker runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolPolicy:
    """Runtime constraints for the worker tool surface."""

    restrict_run_bash: bool = False
    require_wrapped_verify_tools: bool = False
    require_uv_run: bool = False
    deny_shell_file_mutations: bool = False
    deny_shell_commands: tuple[str, ...] = ()

    def as_jsonable(self) -> dict[str, Any]:
        """Serialize for subprocess transport."""
        return asdict(self)

    def to_prompt_lines(self) -> list[str]:
        """Render enabled policy as concise worker-facing prompt lines."""
        lines: list[str] = []
        if self.restrict_run_bash:
            lines.append("run_bash is restricted; prefer dedicated worker tools.")
        if self.require_wrapped_verify_tools:
            lines.append(
                "Use run_tests/lint_check/lint_fix/format_file/type_check, not raw shell."
            )
        if self.require_uv_run:
            lines.append("Python shell commands must use 'uv run'.")
        if self.deny_shell_file_mutations:
            lines.append("Do not mutate repo files via shell commands; use file tools.")
        if self.deny_shell_commands:
            lines.append("Denied shell commands: " + ", ".join(self.deny_shell_commands))
        return lines


def parse_tool_policy(raw: object) -> ToolPolicy:
    """Parse a TOML/JSON object into ToolPolicy with safe defaults."""
    if not isinstance(raw, dict):
        return ToolPolicy()

    deny_shell_commands = raw.get("deny_shell_commands", ())
    if isinstance(deny_shell_commands, list | tuple):
        deny_shell_commands = tuple(
            str(item) for item in deny_shell_commands if isinstance(item, str)
        )
    else:
        deny_shell_commands = ()

    return ToolPolicy(
        restrict_run_bash=bool(raw.get("restrict_run_bash", False)),
        require_wrapped_verify_tools=bool(raw.get("require_wrapped_verify_tools", False)),
        require_uv_run=bool(raw.get("require_uv_run", False)),
        deny_shell_file_mutations=bool(raw.get("deny_shell_file_mutations", False)),
        deny_shell_commands=deny_shell_commands,
    )
