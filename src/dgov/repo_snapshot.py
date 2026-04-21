"""Governor-owned structural snapshot for repo-level diagnostics.

The worker already gets a prompt-oriented repo map. This module gives the
governor a cached structural view keyed by commit so sentrux failures can be
explained with concrete likely offenders instead of a bare count delta.
"""

from __future__ import annotations

import ast
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

_SNAPSHOT_VERSION = 1
_LONG_FUNCTION_LINES = 45
_COMPLEX_FUNCTION_CC = 10
_COG_COMPLEX_FUNCTION = 15
_MAX_REPORT_ITEMS = 5
_IGNORE_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
)


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    path: str
    qualname: str
    lineno: int
    end_lineno: int
    line_count: int
    cyclomatic: int
    cognitive: int
    param_count: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    version: int
    commit_sha: str
    generated_at: float
    functions: tuple[FunctionMetric, ...]
    files: tuple[FileSnapshot, ...]


class _CyclomaticVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.score = 1

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, _BRANCH_NODES):
            self.score += 1
        elif isinstance(node, ast.BoolOp):
            self.score += max(0, len(node.values) - 1)
        super().generic_visit(node)


class _FunctionCollector:
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.symbols: list[str] = []
        self.functions: list[FunctionMetric] = []

    def collect(self, node: ast.AST) -> None:
        self._walk_module(node, ())

    def _walk_module(self, node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qualname = ".".join((*scope, child.name))
                self.symbols.append(f"class {qualname}")
                self._walk_class(child, (*scope, child.name))
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.append(self._metric_for(child, scope))
                self.symbols.append(f"def {'.'.join((*scope, child.name))}")
                self._walk_nested_function(child, (*scope, child.name))

    def _walk_class(self, node: ast.ClassDef, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.append(self._metric_for(child, scope))
                self.symbols.append(f"def {'.'.join((*scope, child.name))}")
                self._walk_nested_function(child, (*scope, child.name))

    def _walk_nested_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: tuple[str, ...],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.append(self._metric_for(child, scope))
                self._walk_nested_function(child, (*scope, child.name))
            elif isinstance(child, ast.ClassDef):
                self._walk_class(child, (*scope, child.name))

    def _metric_for(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: tuple[str, ...],
    ) -> FunctionMetric:
        qualname = ".".join((*scope, node.name))
        end_lineno = getattr(node, "end_lineno", node.lineno)
        return FunctionMetric(
            path=self.relative_path,
            qualname=qualname,
            lineno=node.lineno,
            end_lineno=end_lineno,
            line_count=max(1, end_lineno - node.lineno + 1),
            cyclomatic=_cyclomatic_complexity(node),
            cognitive=_cognitive_complexity(node),
            param_count=len(node.args.args) + len(node.args.kwonlyargs),
        )


def _cyclomatic_complexity(node: ast.AST) -> int:
    visitor = _CyclomaticVisitor()
    visitor.visit(node)
    return visitor.score


def _cognitive_complexity(node: ast.AST, nesting: int = 0) -> int:
    total = 0
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _BRANCH_NODES):
            total += 1 + nesting
            total += _cognitive_complexity(child, nesting + 1)
            continue
        if isinstance(child, ast.BoolOp):
            total += max(0, len(child.values) - 1)
        total += _cognitive_complexity(child, nesting)
    return total


def _iter_python_files(scan_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(scan_root.rglob("*.py")):
        rel_parts = path.relative_to(scan_root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in _IGNORE_PARTS for part in rel_parts):
            continue
        files.append(path)
    return files


def _git_head_sha(scan_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=scan_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _worktree_dirty(scan_root: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=scan_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _snapshot_cache_dir(project_root: Path) -> Path:
    return project_root / ".dgov" / "runtime" / "repo_snapshot"


def _snapshot_cache_path(project_root: Path, commit_sha: str) -> Path:
    return _snapshot_cache_dir(project_root) / f"{commit_sha}.json"


def _object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected list, got {type(value).__name__}")
    return cast("list[object]", value)


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"Expected dict, got {type(value).__name__}")
    return cast("dict[str, object]", value)


def _int_like(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected int-like value, got {type(value).__name__}")


def _float_like(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"Expected float-like value, got {type(value).__name__}")


def _decode_snapshot(data: dict[str, object]) -> RepoSnapshot:
    functions_raw = _object_list(data["functions"])
    files_raw = _object_list(data["files"])
    return RepoSnapshot(
        version=_int_like(data["version"]),
        commit_sha=str(data["commit_sha"]),
        generated_at=_float_like(data["generated_at"]),
        functions=tuple(
            FunctionMetric(
                path=str(item_map["path"]),
                qualname=str(item_map["qualname"]),
                lineno=_int_like(item_map["lineno"]),
                end_lineno=_int_like(item_map["end_lineno"]),
                line_count=_int_like(item_map["line_count"]),
                cyclomatic=_int_like(item_map["cyclomatic"]),
                cognitive=_int_like(item_map["cognitive"]),
                param_count=_int_like(item_map["param_count"]),
            )
            for item_map in (_object_dict(item) for item in functions_raw)
        ),
        files=tuple(
            FileSnapshot(
                path=str(item_map["path"]),
                symbols=tuple(str(symbol) for symbol in _object_list(item_map["symbols"])),
            )
            for item_map in (_object_dict(item) for item in files_raw)
        ),
    )


def build_repo_snapshot(scan_root: Path, *, cache_root: Path | None = None) -> RepoSnapshot:
    """Build and cache a structural snapshot for the current commit."""
    cache_project_root = cache_root or scan_root
    commit_sha = _git_head_sha(scan_root)
    dirty = _worktree_dirty(scan_root)
    cache_path = _snapshot_cache_path(cache_project_root, commit_sha)
    if not dirty and cache_path.exists():
        return _decode_snapshot(json.loads(cache_path.read_text()))

    file_snapshots: list[FileSnapshot] = []
    functions: list[FunctionMetric] = []
    for path in _iter_python_files(scan_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        collector = _FunctionCollector(str(path.relative_to(scan_root)))
        collector.collect(tree)
        file_snapshots.append(
            FileSnapshot(path=collector.relative_path, symbols=tuple(collector.symbols))
        )
        functions.extend(collector.functions)

    snapshot = RepoSnapshot(
        version=_SNAPSHOT_VERSION,
        commit_sha=commit_sha,
        generated_at=time.time(),
        functions=tuple(sorted(functions, key=lambda item: (item.path, item.lineno))),
        files=tuple(sorted(file_snapshots, key=lambda item: item.path)),
    )
    if not dirty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(asdict(snapshot), indent=2) + "\n")
    return snapshot


def likely_structural_offenders(
    scan_root: Path,
    *,
    cache_root: Path | None = None,
    limit: int = _MAX_REPORT_ITEMS,
) -> dict[str, object]:
    """Return likely long/complex function offenders for the current commit."""
    snapshot = build_repo_snapshot(scan_root, cache_root=cache_root)
    long_functions = sorted(
        (fn for fn in snapshot.functions if fn.line_count >= _LONG_FUNCTION_LINES),
        key=lambda fn: (-fn.line_count, -fn.cyclomatic, fn.path, fn.qualname),
    )[:limit]
    complex_functions = sorted(
        (fn for fn in snapshot.functions if fn.cyclomatic >= _COMPLEX_FUNCTION_CC),
        key=lambda fn: (-fn.cyclomatic, -fn.line_count, fn.path, fn.qualname),
    )[:limit]
    cog_complex_functions = sorted(
        (fn for fn in snapshot.functions if fn.cognitive >= _COG_COMPLEX_FUNCTION),
        key=lambda fn: (-fn.cognitive, -fn.line_count, fn.path, fn.qualname),
    )[:limit]
    return {
        "commit_sha": snapshot.commit_sha,
        "long_functions": [asdict(item) for item in long_functions],
        "complex_functions": [asdict(item) for item in complex_functions],
        "cog_complex_functions": [asdict(item) for item in cog_complex_functions],
    }


def _report_header(report: dict[str, object]) -> str:
    commit_sha = str(report.get("commit_sha", "")).strip()
    if commit_sha:
        return f"Likely structural offenders at {commit_sha[:12]}:"
    return "Likely structural offenders:"


def _section_lines(
    items: object,
    *,
    label: str,
    metric: str,
) -> list[str]:
    if not isinstance(items, list) or not items:
        return []

    lines = [f"- {label}:"]
    for raw in items:
        if not isinstance(raw, dict):
            continue
        raw_map = _object_dict(raw)
        path = str(raw_map.get("path") or "")
        qualname = str(raw_map.get("qualname") or "")
        lineno = _int_like(raw_map.get("lineno") or 0)
        value = _int_like(raw_map.get(metric) or 0)
        lines.append(f"  {path}:{lineno} {qualname} ({metric}={value})")
    return lines


def format_structural_offender_report(report: dict[str, object]) -> str:
    """Render a human-readable offender report from snapshot heuristics."""
    sections = [_report_header(report)]

    for key, label, metric in (
        ("complex_functions", "Complex functions", "cyclomatic"),
        ("cog_complex_functions", "Cognitive hotspots", "cognitive"),
        ("long_functions", "Long functions", "line_count"),
    ):
        sections.extend(_section_lines(report.get(key), label=label, metric=metric))
    return "\n".join(sections)


__all__ = [
    "RepoSnapshot",
    "build_repo_snapshot",
    "format_structural_offender_report",
    "likely_structural_offenders",
]
