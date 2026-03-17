"""Configurable monitor hooks via TOML configuration files.

Defines MonitorHook dataclass and functions to load and match hooks
from ~/.dgov/monitor-hooks.toml and <project_root>/.dgov/monitor-hooks.toml.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

KindType = Literal[
    "working",
    "done",
    "stuck",
    "idle",
    "waiting_input",
    "committing",
    "nudge",
    "fail",
    "auto_complete",
]


@dataclass(frozen=True)
class MonitorHook:
    """A monitor hook that triggers on matching output.

    Attributes:
        pattern: Regex pattern to match against worker output.
        kind: The type of action/state this hook represents.
        message: Optional custom message to display or send.
        keystroke: Optional keystroke sequence to send (for nudge actions).
    """

    pattern: str
    kind: KindType
    message: str | None = None
    keystroke: str | None = None


def load_monitor_hooks(session_root: str) -> list[MonitorHook]:
    """Load monitor hooks from config files.

    Loads hooks from two locations, merging them with project-level hooks
    overriding home-level hooks for matching patterns:
        1. ~/.dgov/monitor-hooks.toml (user-level defaults)
        2. <project_root>/.dgov/monitor-hooks.toml (project-specific overrides)

    Args:
        session_root: Path to the session root directory.

    Returns:
        List of MonitorHook instances, merged from both config sources.
    """
    hooks_map: dict[str, MonitorHook] = {}

    def _load_from_file(path: Path) -> None:
        if not path.is_file():
            return
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            # Support both [[hooks.hook]] and [hooks] with sub-tables
            raw_hooks = []
            if "hooks" in data:
                if "hook" in data["hooks"] and isinstance(data["hooks"]["hook"], list):
                    raw_hooks = data["hooks"]["hook"]
                elif isinstance(data["hooks"], dict):
                    # Flatten [hooks.my-rule] into list
                    for k, v in data["hooks"].items():
                        if isinstance(v, dict):
                            raw_hooks.append({"pattern": v.get("pattern", k), **v})

            for entry in raw_hooks:
                pattern = entry.get("pattern")
                if not pattern:
                    continue
                hooks_map[pattern] = MonitorHook(
                    pattern=pattern,
                    kind=entry.get("kind", "working"),
                    message=entry.get("message"),
                    keystroke=entry.get("keystroke"),
                )
        except Exception as exc:
            logger.warning("Failed to load hooks from %s: %s", path, exc)

    # Load home config
    _load_from_file(Path.home() / ".dgov" / "monitor-hooks.toml")
    # Load project config
    _load_from_file(Path(session_root) / ".dgov" / "monitor-hooks.toml")

    return list(hooks_map.values())


def match_monitor_hook(output: str, rules: list[MonitorHook]) -> MonitorHook | None:
    """Check worker output against monitor hook patterns.

    Checks the last 10 lines of output against all registered hook patterns.
    Returns the first matching hook, or None if no match found.
    """
    if not output or not rules:
        return None

    lines = output.strip().splitlines()[-10:]
    tail = "\n".join(lines)

    for hook in rules:
        try:
            if re.search(hook.pattern, tail, re.IGNORECASE):
                return hook
        except re.error:
            continue

    return None
