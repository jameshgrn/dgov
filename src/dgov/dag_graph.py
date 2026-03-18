"""DAG graph algorithms: validation, topological sort, tier computation."""

from __future__ import annotations

from dgov.dag_parser import DagTaskSpec


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
