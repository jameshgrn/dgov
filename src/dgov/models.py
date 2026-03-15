"""Minimal dataclasses for dgov (extracted from dgov.models)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MergeResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    conflicts: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
