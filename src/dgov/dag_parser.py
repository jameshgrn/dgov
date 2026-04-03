"""DAG file dataclasses and TOML parser — minimal governor loop version."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DagFileSpec:
    create: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagTaskSpec:
    slug: str
    summary: str
    prompt: str
    commit_message: str
    agent: str
    escalation: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    files: DagFileSpec = field(default_factory=DagFileSpec)
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


@dataclass(frozen=True)
class DagEvalSpec:
    """A deterministic eval command with a name."""

    name: str
    command: str

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Eval name must not be empty")
        if not self.command or not self.command.strip():
            raise ValueError(f"Eval {self.name!r}: command must not be empty")


@dataclass(frozen=True)
class DagDefinition:
    name: str
    dag_file: str
    project_root: str
    session_root: str
    max_concurrent: int
    tasks: dict[str, DagTaskSpec]
    evals: tuple[DagEvalSpec, ...] = ()

    # Merge / Policy defaults
    default_max_retries: int = 3
    merge_resolve: str = "skip"
    merge_squash: bool = True


def parse_dag_file(path: str) -> DagDefinition:
    """Parse a TOML DAG file into a DagDefinition."""
    raw_bytes = Path(path).read_bytes()
    raw = tomllib.loads(raw_bytes.decode())

    dag_section = raw.get("dag", raw.get("plan", {}))
    if not dag_section:
        raise ValueError("Missing [plan] or [dag] section")
    if "name" not in dag_section:
        raise ValueError("Missing plan.name")

    project_root = dag_section.get("project_root", ".")
    session_root = dag_section.get("session_root", ".")

    tasks: dict[str, DagTaskSpec] = {}
    tasks_raw = raw.get("tasks", raw.get("units", {}))
    for slug, task_raw in tasks_raw.items():
        tasks[slug] = _parse_task(slug, task_raw)

    evals: list[DagEvalSpec] = []
    evals_raw = raw.get("evals", [])
    if isinstance(evals_raw, list):
        for ev in evals_raw:
            evals.append(DagEvalSpec(name=ev["id"], command=ev["evidence"]))
    elif isinstance(evals_raw, dict):
        for name, command in evals_raw.items():
            evals.append(DagEvalSpec(name=name, command=command))

    return DagDefinition(
        name=dag_section["name"],
        dag_file=str(Path(path).resolve()),
        project_root=project_root,
        session_root=session_root,
        max_concurrent=dag_section.get("max_concurrent", 0),
        tasks=tasks,
        evals=tuple(evals),
        default_max_retries=dag_section.get("default_max_retries", 3),
        merge_resolve=dag_section.get("merge_resolve", "skip"),
        merge_squash=dag_section.get("merge_squash", True),
    )


def _parse_task(slug: str, raw: dict) -> DagTaskSpec:
    """Parse and validate a single task block."""
    for req in ("summary", "prompt", "commit_message"):
        if not raw.get(req):
            raise ValueError(f"Task {slug!r}: missing required field {req!r}")

    files_raw = raw.get("files", {})
    files = DagFileSpec(
        create=tuple(files_raw.get("create", [])),
        edit=tuple(files_raw.get("edit", [])),
        delete=tuple(files_raw.get("delete", [])),
    )

    acceptance = raw.get("acceptance", {})

    return DagTaskSpec(
        slug=slug,
        summary=raw["summary"],
        prompt=raw["prompt"],
        commit_message=raw["commit_message"],
        agent=raw.get("agent", "worker"),
        escalation=tuple(raw.get("escalation", ())),
        depends_on=tuple(raw.get("depends_on", ())),
        files=files,
        timeout_s=raw.get("timeout_s", 900),
        permission_mode=raw.get("permission_mode", "bypassPermissions"),
        template=raw.get("template"),
        template_vars=raw.get("template_vars"),
        tests_pass=bool(acceptance.get("tests_pass", True)),
        lint_clean=bool(acceptance.get("lint_clean", True)),
        post_merge_check=acceptance.get("post_merge_check") or acceptance.get("custom_check"),
        review_agent=raw.get("review_agent"),
        role=raw.get("role", "worker"),
    )
