"""Minimal dataclasses for dgov (extracted from dgov.models)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskSpec:
    id: str
    description: str
    exports: list[str]
    imports: list[str]
    touches: list[str]
    body: str
    timeout: int | None = None
    worker_cmd: str | None = None
    resource_class: str | None = None
    domain: str | None = None
    provider: str | None = None
    after: list[str] = field(default_factory=list)
    expects_changes: bool = False
    permission_mode: str = "acceptEdits"


@dataclass
class ConflictDetails:
    file_path: str
    base: str
    head: str
    branch: str


@dataclass
class MergeResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    conflicts: list[ConflictDetails] = field(default_factory=list)
