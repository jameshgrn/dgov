"""DAG file dataclasses and TOML parser."""

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
    escalation: tuple[str, ...]
    depends_on: tuple[str, ...]
    files: DagFileSpec
    permission_mode: str
    timeout_s: int
    post_merge_check: str = ""
    review_agent: str = ""  # model for reviewing this task's output


@dataclass(frozen=True)
class DagDefinition:
    name: str
    dag_file: str
    project_root: str
    session_root: str
    default_max_retries: int
    merge_resolve: str
    merge_squash: bool
    max_concurrent: int
    tasks: dict[str, DagTaskSpec]


@dataclass(frozen=True)
class DagRunOptions:
    dry_run: bool = False
    tier_limit: int | None = None
    skip: frozenset[str] = frozenset()
    max_retries: int = 1
    auto_merge: bool = True
    max_concurrent: int = 0


@dataclass
class DagRunSummary:
    run_id: int
    dag_file: str
    status: str
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalated: list[dict[str, object]] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    unmerged: list[str] = field(default_factory=list)


def parse_dag_file(path: str) -> DagDefinition:
    """Parse a TOML DAG file into a DagDefinition."""
    raw_bytes = Path(path).read_bytes()
    raw = tomllib.loads(raw_bytes.decode())
    dag_section = raw.get("dag")
    if not dag_section:
        raise ValueError("Missing [dag] section")
    if "version" not in dag_section:
        raise ValueError("Missing dag.version")
    if "name" not in dag_section:
        raise ValueError("Missing dag.name")
    tasks_raw = raw.get("tasks")
    if not tasks_raw:
        raise ValueError("Missing [tasks] section")

    project_root = dag_section.get("project_root", ".")
    session_root = dag_section.get("session_root", ".")
    defaults = {
        "permission_mode": dag_section.get("default_permission_mode", "bypassPermissions"),
        "timeout_s": dag_section.get("default_timeout_s", 900),
    }

    tasks: dict[str, DagTaskSpec] = {}
    for slug, task_raw in tasks_raw.items():
        tasks[slug] = _parse_task(slug, task_raw, defaults, path, project_root, session_root)

    return DagDefinition(
        name=dag_section["name"],
        dag_file=str(Path(path).resolve()),
        project_root=project_root,
        session_root=session_root,
        default_max_retries=dag_section.get("default_max_retries", 1),
        merge_resolve=dag_section.get("merge_resolve", "skip"),
        merge_squash=dag_section.get("merge_squash", True),
        max_concurrent=dag_section.get("max_concurrent", 0),
        tasks=tasks,
    )


def _parse_task(
    slug: str,
    raw: dict,
    defaults: dict,
    dag_file: str,
    project_root: str,
    session_root: str,
) -> DagTaskSpec:
    """Parse and validate a single task block."""
    for req in ("summary", "agent", "prompt", "commit_message"):
        if not raw.get(req):
            raise ValueError(f"Task {slug!r}: missing required field {req!r}")
    if not raw.get("prompt", "").strip():
        raise ValueError(f"Task {slug!r}: prompt must not be empty")

    files_raw = raw.get("files", {})
    files = _normalize_file_specs(project_root, files_raw)
    if not files.create and not files.edit and not files.delete:
        raise ValueError(f"Task {slug!r}: must specify at least one file in create/edit/delete")

    return DagTaskSpec(
        slug=slug,
        summary=raw["summary"],
        prompt=raw["prompt"],
        commit_message=raw["commit_message"],
        agent=raw["agent"],
        escalation=tuple(raw.get("escalation", ())),
        depends_on=tuple(raw.get("depends_on", ())),
        files=files,
        permission_mode=raw.get("permission_mode", defaults["permission_mode"]),
        timeout_s=raw.get("timeout_s", defaults["timeout_s"]),
        post_merge_check=raw.get("post_merge_check", ""),
        review_agent=str(raw.get("review_agent", "")),
    )


def _normalize_file_specs(project_root: str, files: dict) -> DagFileSpec:
    """Normalize file spec lists: validate no globs, all relative, sort."""
    result = {}
    for key in ("create", "edit", "delete"):
        paths = files.get(key, [])
        for p in paths:
            if "*" in p or "?" in p or "[" in p:
                raise ValueError(f"Globs not allowed in file specs: {p!r}")
            if Path(p).is_absolute():
                raise ValueError(f"File paths must be relative: {p!r}")
        result[key] = tuple(sorted(paths))
    return DagFileSpec(**result)
