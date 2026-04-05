"""DAG file models and TOML parser via Pydantic V2.

Pillar #4: Determinism - Validates all plan inputs before execution starts.
Pillar #10: Fail-Closed - Rejects invalid TOML schemas immediately.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class DagFileSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()


class DagTaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    slug: str
    summary: str
    prompt: str
    commit_message: str
    agent: str = "worker"
    depends_on: tuple[str, ...] = ()
    files: DagFileSpec = Field(default_factory=DagFileSpec)
    timeout_s: int = 900

    def all_touches(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.files.create, *self.files.edit, *self.files.delete)))


class DagDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    dag_file: str
    project_root: str = "."
    session_root: str = "."
    tasks: dict[str, DagTaskSpec]
    default_max_retries: int = 3


def parse_dag_file(path: str) -> DagDefinition:
    """Parse and validate a TOML DAG file into a DagDefinition."""
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")

    raw = tomllib.loads(path_obj.read_text())

    plan_section = raw.get("plan", {})
    if not plan_section:
        raise ValueError("Missing [plan] section")

    tasks_raw = raw.get("tasks", {})
    if not tasks_raw:
        raise ValueError("Missing [tasks] section")

    # Process tasks to include slug
    tasks: dict[str, Any] = {}
    for slug, task_data in tasks_raw.items():
        if isinstance(task_data, dict):
            task_data["slug"] = slug
            tasks[slug] = task_data

    return DagDefinition(
        name=plan_section.get("name", "unnamed-plan"),
        dag_file=str(path_obj.resolve()),
        project_root=plan_section.get("project_root", "."),
        session_root=plan_section.get("session_root", "."),
        tasks=tasks,
        default_max_retries=plan_section.get("default_max_retries", 3),
    )
