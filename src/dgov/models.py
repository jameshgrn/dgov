"""Minimal dataclasses for dgov (extracted from dgov.models)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


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
class TddProgress:
    """Structured TDD protocol progress reported by worker agents."""

    step: int
    step_name: str
    iteration: int
    max_iterations: int
    tests_passed: int
    tests_failed: int
    tests_total: int
    elapsed_s: float
    escalation_needed: bool = False
    failing_tests: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> TddProgress | None:
        """Read TDD progress JSON from *path*. Returns None on missing/invalid."""
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls(
                step=int(data.get("step", 0)),
                step_name=str(data.get("step_name", "")),
                iteration=int(data.get("iteration", 0)),
                max_iterations=int(data.get("max_iterations", 0)),
                tests_passed=int(data.get("tests_passed", 0)),
                tests_failed=int(data.get("tests_failed", 0)),
                tests_total=int(data.get("tests_total", 0)),
                elapsed_s=float(data.get("elapsed_s", 0.0)),
                escalation_needed=bool(data.get("escalation_needed", False)),
                failing_tests=list(data.get("failing_tests", [])),
            )
        except (TypeError, ValueError):
            return None

    def to_file(self, path: Path) -> None:
        """Write TDD progress JSON to *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


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
