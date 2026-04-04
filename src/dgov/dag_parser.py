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
    model_config = ConfigDict(frozen=True)
    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()


class DagTaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    slug: str
    summary: str
    prompt: str
    commit_message: str
    agent: str = "worker"
    escalation: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    files: DagFileSpec = Field(default_factory=DagFileSpec)
    timeout_s: int = 900
    permission_mode: str = "bypassPermissions"
    template: str | None = None
    template_vars: dict[str, str] | None = None

    # Acceptance criteria (hardened post-merge truth)
    tests_pass: bool = True
    lint_clean: bool = True
    post_merge_check: str | None = None
    review_agent: str | None = None
    role: str = "worker"

    def all_touches(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.files.create, *self.files.edit, *self.files.delete)))


class DagEvalSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    kind: str = "smoke"
    statement: str
    evidence: str


class DagDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    dag_file: str
    project_root: str = "."
    session_root: str = "."
    max_concurrent: int = 0
    tasks: dict[str, DagTaskSpec]
    evals: tuple[DagEvalSpec, ...] = ()

    # Merge / Policy defaults
    default_max_retries: int = 3
    merge_resolve: str = "skip"
    merge_squash: bool = True


def parse_dag_file(path: str) -> DagDefinition:
    """Parse and validate a TOML DAG file into a DagDefinition."""
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")

    raw = tomllib.loads(path_obj.read_text())

    # Support both [plan] and [dag] naming
    plan_section = raw.get("plan", raw.get("dag", {}))
    if not plan_section:
        raise ValueError("Missing [plan] or [dag] section")

    # Support both [units] and [tasks] naming
    tasks_raw = raw.get("units", raw.get("tasks", {}))
    if not tasks_raw:
        raise ValueError("Missing [units] or [tasks] section")

    # Process tasks to include slug
    tasks: dict[str, Any] = {}
    for slug, task_data in tasks_raw.items():
        if isinstance(task_data, dict):
            task_data["slug"] = slug
            # Flatten 'acceptance' sub-dictionary if it exists
            if "acceptance" in task_data:
                acc = task_data.pop("acceptance")
                task_data.update(acc)
            tasks[slug] = task_data

    # Map evals
    evals_raw = raw.get("evals", [])

    # Construct the final DagDefinition
    return DagDefinition(
        name=plan_section.get("name", "unnamed-plan"),
        dag_file=str(path_obj.resolve()),
        project_root=plan_section.get("project_root", "."),
        session_root=plan_section.get("session_root", "."),
        max_concurrent=plan_section.get("max_concurrent", 0),
        tasks=tasks,
        evals=evals_raw,
        default_max_retries=plan_section.get("default_max_retries", 3),
        merge_resolve=plan_section.get("merge_resolve", "skip"),
        merge_squash=plan_section.get("merge_squash", True),
    )
