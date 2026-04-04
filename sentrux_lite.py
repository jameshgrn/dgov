#!/usr/bin/env python3
"""
Minimal import graph analyzer (sentrux-lite).
Analyzes src/dgov/ for:
- Import cycles (top-level only - deferred imports don't create cycles)
- Max import depth
- God files (>500 lines)
- Fan-out (top-level imports only)
"""

import ast
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


def parse_top_level_imports(filepath: Path) -> Tuple[List[str], List[str]]:
    """Extract (absolute_imports, relative_imports) from top-level only (for cycle detection)."""
    try:
        content = filepath.read_text()
        tree = ast.parse(content)
    except Exception:
        return [], []

    abs_imports = []
    rel_imports = []

    for node in tree.body:  # Only top-level nodes, not nested in functions/classes
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("dgov"):
                names = [alias.name for alias in node.names]
                abs_imports.append(f"{node.module}:{','.join(names)}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dgov"):
                    abs_imports.append(alias.name)

    return abs_imports, rel_imports


def build_graph(src_dir: Path) -> Dict[str, Set[str]]:
    """Build import dependency graph: module -> set of imported modules (top-level only)."""
    graph = {}
    for pyfile in src_dir.rglob("*.py"):
        if pyfile.name.startswith("_"):
            continue
        # Compute module name from relative path: persistence/tasks.py -> dgov.persistence.tasks
        rel_path = pyfile.relative_to(src_dir)
        parts = rel_path.with_suffix("").parts
        module = f"dgov.{'.'.join(parts)}"
        imports, _ = parse_top_level_imports(pyfile)
        deps = set()
        for imp in imports:
            dep_mod = imp.split(":")[0]
            if dep_mod.startswith("dgov."):
                deps.add(dep_mod)
        graph[module] = deps
    return graph


def find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Find all import cycles using DFS."""
    cycles = []
    visited = set()
    rec_stack = set()
    path = []

    def dfs(node: str):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                # Found cycle
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)

        path.pop()
        rec_stack.remove(node)

    for node in graph:
        if node not in visited:
            dfs(node)

    return cycles


def compute_depth(
    graph: Dict[str, Set[str]], start: str, memo: Dict[str, int] = None, visiting: Set[str] = None
) -> int:
    """Compute max import depth from a starting module."""
    if memo is None:
        memo = {}
    if visiting is None:
        visiting = set()

    if start in memo:
        return memo[start]
    if start in visiting:
        # Cycle detected - return 0 for cycle contribution
        return 0

    deps = graph.get(start, set())
    if not deps:
        memo[start] = 1
        return 1

    visiting.add(start)
    max_dep_depth = 0
    for dep in deps:
        dep_depth = compute_depth(graph, dep, memo, visiting)
        max_dep_depth = max(max_dep_depth, dep_depth)
    visiting.remove(start)

    memo[start] = max_dep_depth + 1
    return memo[start]


def find_god_files(src_dir: Path, max_lines: int = 500) -> List[Tuple[str, int]]:
    """Find files exceeding line limit."""
    gods = []
    for pyfile in src_dir.rglob("*.py"):
        if pyfile.name.startswith("_"):
            continue
        line_count = len(pyfile.read_text().splitlines())
        if line_count > max_lines:
            rel_path = pyfile.relative_to(src_dir)
            gods.append((str(rel_path), line_count))
    return sorted(gods, key=lambda x: -x[1])


def compute_fan_out(src_dir: Path) -> List[Tuple[str, int]]:
    """Compute fan-out (number of top-level imports) per file."""
    fanouts = []
    for pyfile in src_dir.rglob("*.py"):
        if pyfile.name.startswith("_"):
            continue
        imports, _ = parse_top_level_imports(pyfile)
        rel_path = pyfile.relative_to(src_dir)
        fanouts.append((str(rel_path), len(imports)))
    return sorted(fanouts, key=lambda x: -x[1])


def main():
    src_dir = Path("src/dgov")
    if not src_dir.exists():
        print(f"Directory not found: {src_dir}")
        sys.exit(1)

    print("=== SENTRUX-LITE ANALYSIS ===\n")

    # Build graph
    graph = build_graph(src_dir)
    print(f"Modules analyzed: {len(graph)}")

    # Find cycles
    cycles = find_cycles(graph)
    print(f"\n--- CYCLES ({len(cycles)} found) ---")
    for i, cycle in enumerate(cycles, 1):
        print(f"  {i}. {' -> '.join(cycle)}")

    # Compute max depth
    max_depth = 0
    deepest = None
    for mod in graph:
        depth = compute_depth(graph, mod)
        if depth > max_depth:
            max_depth = depth
            deepest = mod
    print("\n--- DEPTH ---")
    print(f"  Max depth: {max_depth} (from {deepest})")

    # God files
    gods = find_god_files(src_dir, max_lines=500)
    print(f"\n--- GOD FILES ({len(gods)} > 500 lines) ---")
    for name, lines in gods:
        print(f"  {name}: {lines} lines")

    # Fan out
    fanouts = compute_fan_out(src_dir)
    print("\n--- FAN OUT (top 10) ---")
    for name, count in fanouts[:10]:
        marker = " **" if count > 15 else ""
        print(f"  {name}: {count}{marker}")

    # Summary
    print("\n=== SUMMARY ===")
    print(f"  Cycles: {len(cycles)}")
    print(f"  Max depth: {max_depth}")
    print(f"  God files: {len(gods)}")
    print(f"  High fan-out (>15): {sum(1 for _, c in fanouts if c > 15)}")

    # Write JSON for tracking
    import json

    report = {
        "cycle_count": len(cycles),
        "cycles": [" -> ".join(c) for c in cycles],
        "max_depth": max_depth,
        "deepest_module": deepest,
        "god_file_count": len(gods),
        "god_files": [{"file": n, "lines": l} for n, l in gods],
        "high_fan_out": [{"file": n, "imports": c} for n, c in fanouts if c > 15],
    }
    Path(".sentrux").mkdir(exist_ok=True)
    Path(".sentrux/current.json").write_text(json.dumps(report, indent=2))
    print("\nReport written to: .sentrux/current.json")

    # Convergence check
    print("\n=== CONVERGENCE TARGETS ===")
    print(f"  Cycles: {len(cycles)} -> 0 (FIX{'ED' if len(cycles) == 0 else ' NEEDED'})")
    print(f"  Depth: {max_depth} -> <= 15 ({'OK' if max_depth <= 15 else 'NEEDS WORK'})")
    print(f"  God files: {len(gods)} -> 0 ({'OK' if len(gods) == 0 else 'NEEDS WORK'})")


if __name__ == "__main__":
    main()
