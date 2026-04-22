"""DAG file models and TOML parser via Pydantic V2.

Pillar #4: Determinism - Validates all plan inputs before execution starts.
Pillar #10: Fail-Closed - Rejects invalid TOML schemas immediately.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

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
    read: tuple[str, ...] = ()
    touch: tuple[str, ...] = ()


class DagTaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    slug: str
    summary: str
    prompt: str | None = None
    prompt_file: str | None = None
    commit_message: str | None = None
    agent: str | None = None
    role: Literal["worker", "researcher", "reviewer"] = "worker"
    depends_on: tuple[str, ...] = ()
    files: DagFileSpec = Field(default_factory=DagFileSpec)
    timeout_s: int = 900
    iteration_budget: int | None = None
    test_cmd: str | None = None
    sop_mapping: tuple[str, ...] = ()

    def all_touches(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((
                *self.files.create,
                *self.files.edit,
                *self.files.delete,
                *self.files.touch,
            ))
        )


class DagDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    dag_file: str
    project_root: str = "."
    session_root: str = "."
    tasks: dict[str, DagTaskSpec]
    default_agent: str = ""
    default_max_retries: int = 3
    source_mtime_max: str = ""
    sop_set_hash: str = ""


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
            # Flat files shorthand: files = ["a.py", "b.py"] → files.touch
            files_val = task_data.get("files")
            if isinstance(files_val, list):
                task_data["files"] = {"touch": files_val}
            tasks[slug] = task_data

    return DagDefinition(
        name=plan_section.get("name", "unnamed-plan"),
        dag_file=str(path_obj.resolve()),
        project_root=plan_section.get("project_root", "."),
        session_root=plan_section.get("session_root", "."),
        tasks=tasks,
        default_agent=plan_section.get("default_agent", ""),
        default_max_retries=plan_section.get(
            "default_max_retries",
            plan_section.get("max_retries", 3),
        ),
        source_mtime_max=plan_section.get("source_mtime_max", ""),
        sop_set_hash=plan_section.get("sop_set_hash", ""),
    )
