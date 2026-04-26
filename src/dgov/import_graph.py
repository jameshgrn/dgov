"""Compile-time Python import graph heuristics."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dgov.dag_parser import DagDefinition, DagTaskSpec


@dataclass(frozen=True)
class ImportConflict:
    """Potential cross-task conflict through a Python import edge."""

    task_a: str
    task_b: str
    written_file: str
    importing_file: str
    via_import: str


def _normalize_rel_path(project_root: Path, path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(project_root)
        except ValueError:
            return ""
    normalized = PurePosixPath(candidate.as_posix()).as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


def _candidate_import_roots(project_root: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for name in ("src", "lib"):
        path = project_root / name
        if path.is_dir():
            roots.append(path)
    roots.append(project_root)
    return tuple(dict.fromkeys(roots))


def _module_path_candidates(parts: Sequence[str]) -> tuple[PurePosixPath, ...]:
    if not parts:
        return ()
    module_path = PurePosixPath(*parts)
    return (
        module_path.with_suffix(".py"),
        module_path / "__init__.py",
    )


def _resolve_module(project_root: Path, module: str) -> str | None:
    parts = tuple(part for part in module.split(".") if part)
    for root in _candidate_import_roots(project_root):
        for candidate in _module_path_candidates(parts):
            full_path = root / candidate
            if full_path.is_file():
                return _normalize_rel_path(project_root, full_path)
    return None


def _resolve_rel_candidate(project_root: Path, rel_path: PurePosixPath) -> str | None:
    for candidate in (rel_path.with_suffix(".py"), rel_path / "__init__.py"):
        full_path = project_root / candidate
        if full_path.is_file():
            return _normalize_rel_path(project_root, full_path)
    return None


def _resolve_absolute_from(
    project_root: Path,
    module: str | None,
    names: Sequence[ast.alias],
) -> set[str]:
    module = module or ""
    imports: set[str] = set()

    for alias in names:
        if alias.name == "*":
            continue
        candidate = f"{module}.{alias.name}" if module else alias.name
        resolved = _resolve_module(project_root, candidate)
        if resolved is not None:
            imports.add(resolved)

    if imports:
        return imports

    if module:
        resolved = _resolve_module(project_root, module)
        if resolved is not None:
            imports.add(resolved)
    return imports


def _relative_base(importer: str, level: int) -> PurePosixPath | None:
    rel = PurePosixPath(importer)
    base = rel.parent
    if rel.name == "__init__.py":
        base = rel.parent
    for _ in range(max(level - 1, 0)):
        if not base.parts:
            return None
        base = base.parent
    return base


def _resolve_relative_from(
    project_root: Path,
    importer: str,
    module: str | None,
    names: Sequence[ast.alias],
    level: int,
) -> set[str]:
    base = _relative_base(importer, level)
    if base is None:
        return set()

    module_path = base
    if module:
        module_path = module_path.joinpath(*module.split("."))

    imports: set[str] = set()
    for alias in names:
        if alias.name == "*":
            continue
        resolved = _resolve_rel_candidate(project_root, module_path / alias.name)
        if resolved is not None:
            imports.add(resolved)

    if imports:
        return imports

    resolved = _resolve_rel_candidate(project_root, module_path)
    return {resolved} if resolved is not None else set()


def _extract_imports(project_root: Path, importer: str, tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_module(project_root, alias.name)
                if resolved is not None:
                    imports.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imports.update(
                    _resolve_relative_from(
                        project_root,
                        importer,
                        node.module,
                        node.names,
                        node.level,
                    )
                )
            else:
                imports.update(_resolve_absolute_from(project_root, node.module, node.names))
    return imports


def build_import_graph(
    project_root: str,
    python_files: Sequence[str],
) -> dict[str, set[str]]:
    """Build a Python-file import adjacency list from the working tree."""
    root = Path(project_root).resolve()
    queue = [
        rel
        for path in python_files
        if (rel := _normalize_rel_path(root, path)) and rel.endswith(".py")
    ]
    seen: set[str] = set()
    graph: dict[str, set[str]] = {}

    while queue:
        rel = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)

        full_path = root / rel
        if not full_path.is_file():
            continue

        try:
            tree = ast.parse(full_path.read_text(encoding="utf-8"), filename=str(full_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            graph[rel] = set()
            continue

        imports = _extract_imports(root, rel, tree)
        graph[rel] = imports
        for imported in sorted(imports):
            if imported not in seen:
                queue.append(imported)

    return graph


def _task_write_set(task: DagTaskSpec) -> set[str]:
    return {
        _normalize_claim(path)
        for path in (*task.files.create, *task.files.edit, *task.files.delete, *task.files.touch)
        if path.strip() and _normalize_claim(path).endswith(".py")
    }


def _normalize_claim(path: str) -> str:
    normalized = path.strip().lstrip("./").rstrip("/")
    return PurePosixPath(normalized).as_posix()


def _reachable_deps(slug: str, tasks: dict[str, DagTaskSpec]) -> set[str]:
    visited: set[str] = set()
    stack = list(tasks[slug].depends_on) if slug in tasks else []
    while stack:
        dep = stack.pop()
        if dep in visited:
            continue
        visited.add(dep)
        if dep in tasks:
            stack.extend(tasks[dep].depends_on)
    return visited


def _independent(a: str, b: str, tasks: dict[str, DagTaskSpec]) -> bool:
    return b not in _reachable_deps(a, tasks) and a not in _reachable_deps(b, tasks)


def _conflicts_one_way(
    *,
    writer: str,
    importer: str,
    writer_files: Iterable[str],
    importer_files: Iterable[str],
    import_graph: dict[str, set[str]],
) -> list[ImportConflict]:
    conflicts: list[ImportConflict] = []
    for written_file in sorted(writer_files):
        for importing_file in sorted(importer_files):
            if written_file not in import_graph.get(importing_file, set()):
                continue
            conflicts.append(
                ImportConflict(
                    task_a=writer,
                    task_b=importer,
                    written_file=written_file,
                    importing_file=importing_file,
                    via_import=written_file,
                )
            )
    return conflicts


def detect_cross_task_import_conflicts(
    dag: DagDefinition,
    import_graph: dict[str, set[str]],
) -> list[ImportConflict]:
    """Detect independent tasks whose written files import each other's writes."""
    conflicts: list[ImportConflict] = []
    write_sets = {slug: _task_write_set(task) for slug, task in dag.tasks.items()}
    slugs = list(dag.tasks)

    for i, slug_a in enumerate(slugs):
        writes_a = write_sets[slug_a]
        if not writes_a:
            continue
        for slug_b in slugs[i + 1 :]:
            writes_b = write_sets[slug_b]
            if not writes_b or not _independent(slug_a, slug_b, dag.tasks):
                continue
            conflicts.extend(
                _conflicts_one_way(
                    writer=slug_a,
                    importer=slug_b,
                    writer_files=writes_a,
                    importer_files=writes_b,
                    import_graph=import_graph,
                )
            )
            conflicts.extend(
                _conflicts_one_way(
                    writer=slug_b,
                    importer=slug_a,
                    writer_files=writes_b,
                    importer_files=writes_a,
                    import_graph=import_graph,
                )
            )

    return conflicts
