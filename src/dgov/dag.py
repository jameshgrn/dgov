"""DAG file parser and execution engine for dgov."""

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


@dataclass(frozen=True)
class DagDefinition:
    name: str
    dag_file: str
    project_root: str
    session_root: str
    default_max_retries: int
    merge_resolve: str
    merge_squash: bool
    tasks: dict[str, DagTaskSpec]


@dataclass(frozen=True)
class DagRunOptions:
    dry_run: bool = False
    tier_limit: int | None = None
    skip: frozenset[str] = frozenset()
    max_retries: int = 1
    auto_merge: bool = True


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
        "permission_mode": dag_section.get("default_permission_mode", "acceptEdits"),
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


def validate_dag(tasks: dict[str, DagTaskSpec]) -> None:
    """Validate depends_on references exist and there are no cycles."""
    task_ids = set(tasks)
    for slug, task in tasks.items():
        for dep in task.depends_on:
            if dep not in task_ids:
                raise ValueError(f"Task {slug!r} depends on {dep!r} which does not exist")

    visited: set[str] = set()
    path: set[str] = set()

    def _visit(node: str) -> None:
        if node in path:
            raise ValueError(f"Dependency cycle detected involving {node!r}")
        if node in visited:
            return
        path.add(node)
        for dep in tasks[node].depends_on:
            _visit(dep)
        path.discard(node)
        visited.add(node)

    for tid in tasks:
        _visit(tid)


def topological_order(tasks: dict[str, DagTaskSpec]) -> list[str]:
    """Return task slugs in stable topological order."""
    validate_dag(tasks)
    visited: set[str] = set()
    order: list[str] = []

    def _visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(tasks[node].depends_on):
            _visit(dep)
        order.append(node)

    for tid in sorted(tasks):
        _visit(tid)
    return order


def _touches(task: DagTaskSpec) -> set[str]:
    """Return the union of all file specs for overlap checking."""
    return set(task.files.create) | set(task.files.edit) | set(task.files.delete)


def _paths_overlap(a: str, b: str) -> bool:
    """True if paths conflict: exact match, or ancestor/descendant."""
    if a == b:
        return True
    a_clean = a.rstrip("/")
    b_clean = b.rstrip("/")
    return a_clean.startswith(b_clean + "/") or b_clean.startswith(a_clean + "/")


def compute_tiers(tasks: dict[str, DagTaskSpec]) -> list[list[str]]:
    """Group tasks into parallel tiers respecting deps and file overlap."""
    validate_dag(tasks)
    placed: dict[str, int] = {}
    tiers: list[list[str]] = []
    remaining = set(tasks)

    while remaining:
        tier: list[str] = []
        tier_touches: set[str] = set()
        placed_this_round: list[str] = []

        for slug in sorted(remaining):
            task = tasks[slug]
            if not all(d in placed for d in task.depends_on):
                continue
            task_files = _touches(task)
            has_overlap = False
            for tf in task_files:
                for et in tier_touches:
                    if _paths_overlap(tf, et):
                        has_overlap = True
                        break
                if has_overlap:
                    break
            if has_overlap:
                continue
            tier.append(slug)
            tier_touches.update(task_files)
            placed_this_round.append(slug)

        if not placed_this_round:
            raise ValueError(f"Cannot schedule remaining tasks: {remaining}")

        tier_idx = len(tiers)
        for slug in placed_this_round:
            placed[slug] = tier_idx
            remaining.discard(slug)
        tiers.append(tier)

    return tiers


def transitive_dependents(tasks: dict[str, DagTaskSpec], failed: set[str]) -> set[str]:
    """Return all task slugs that transitively depend on any failed slug."""
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for slug, task in tasks.items():
            if slug in dependents or slug in failed:
                continue
            if any(d in failed or d in dependents for d in task.depends_on):
                dependents.add(slug)
                changed = True
    return dependents


def render_dry_run(tiers: list[list[str]], tasks: dict[str, DagTaskSpec]) -> str:
    """Render a human-readable tier listing."""
    total = sum(len(t) for t in tiers)
    lines = [f"DAG ({total} tasks, {len(tiers)} tiers):", ""]
    for i, tier in enumerate(tiers):
        slugs = ", ".join(tier)
        lines.append(f"  Tier {i}: {slugs}")
        for slug in tier:
            task = tasks[slug]
            lines.append(f"    {slug}: {task.summary} [{task.agent}]")
    return "\n".join(lines)
