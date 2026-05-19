"""Plan tree walker + merger + resolver + validator + serializer.

Pillar #4: Determinism - Validates all structural invariants before compile output.
Pillar #10: Fail-Closed - Rejects cycles, unreachable units, bad refs immediately.

Walker reads `_root.toml` + section directories into a PlanTree.
Merger flattens the tree into a FlatPlan with path-qualified unit IDs.
Resolver rewrites each unit's `depends_on` to fully-qualified IDs.
Validator runs structural DAG checks (cycles, unreachability).
Serializer writes dispatch-ready `_compiled.toml` in flat PlanSpec format.
See .dgov/plans/plan-system/DESIGN.md for the full compile pipeline.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal, cast

from dgov.plan import PlanUnit, PlanUnitFiles

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class RootMeta:
    """Parsed `[plan]` section of `_root.toml`."""

    name: str
    summary: str
    sections: tuple[str, ...]
    default_agent: str = ""
    default_provider: str = ""


@dataclass(frozen=True)
class PlanTree:
    """Result of walking a plan tree (depth-1, flat per section)."""

    plan_root: Path
    root_meta: RootMeta
    section_files: dict[str, tuple[Path, ...]]


@dataclass(frozen=True)
class FlatPlan:
    """Result of merging a PlanTree into path-qualified units.

    Before `resolve_refs`, `depends_on` values on units are raw strings from
    source TOMLs (bare or path-qualified). After `resolve_refs`, all values
    are fully-qualified IDs.
    """

    plan_root: Path
    root_meta: RootMeta
    units: dict[str, PlanUnit]  # fq_id -> unit
    source_map: dict[str, Path]  # fq_id -> source TOML path
    source_mtime_max: float


def _load_root_meta(root_file: Path) -> RootMeta:
    raw = tomllib.loads(root_file.read_text())
    plan_section = raw.get("plan")
    if not isinstance(plan_section, dict) or not plan_section:
        raise ValueError(f"_root.toml missing [plan] section: {root_file}")

    name = _root_string(plan_section, "name", root_file, required=True)

    sections = plan_section.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError(f"_root.toml [plan].sections must be a list: {root_file}")
    if not all(isinstance(s, str) for s in sections):
        raise ValueError(f"_root.toml [plan].sections must contain only strings: {root_file}")
    invalid_sections = [s for s in sections if not _SLUG_RE.match(s)]
    if invalid_sections:
        raise ValueError(
            "_root.toml [plan].sections contains invalid section names "
            f"{invalid_sections}: section names must match {_SLUG_RE.pattern}"
        )

    return RootMeta(
        name=name,
        summary=_root_string(plan_section, "summary", root_file),
        sections=tuple(sections),
        default_agent=_root_string(plan_section, "default_agent", root_file),
        default_provider=_root_string(plan_section, "default_provider", root_file),
    )


def _root_string(
    plan_section: dict[str, Any],
    key: str,
    root_file: Path,
    *,
    required: bool = False,
) -> str:
    if key not in plan_section:
        if required:
            raise ValueError(f"_root.toml [plan] missing '{key}': {root_file}")
        return ""
    value = plan_section[key]
    if not isinstance(value, str):
        raise ValueError(f"_root.toml [plan].{key} must be a string: {root_file}")
    if required and not value.strip():
        raise ValueError(f"_root.toml [plan].{key} must be a non-empty string: {root_file}")
    return value


def _section_unit_files(plan_root: Path, section: str) -> tuple[Path, ...]:
    section_dir = plan_root / section
    if not section_dir.is_dir():
        raise ValueError(
            f"_root.toml [plan].sections declares section {section!r}, "
            f"but no section directory exists at {section_dir}. "
            f"Create {section_dir}, or remove {section!r} from [plan].sections."
        )
    files = sorted(
        p
        for p in section_dir.iterdir()
        if p.is_file()
        and p.suffix == ".toml"
        and not p.name.startswith(".")
        and not p.name.startswith("_")
    )
    return tuple(files)


def walk_tree(plan_root: Path) -> PlanTree:
    """Walk a plan tree rooted at plan_root.

    Reads `_root.toml`, enumerates declared sections, collects depth-1 `*.toml`
    files per section (excluding hidden and `_`-prefixed files, excluding
    subdirectories).

    Raises:
        FileNotFoundError: `_root.toml` missing.
        ValueError: invalid metadata, or declared section missing its directory.
    """
    root_file = plan_root / "_root.toml"
    if not root_file.exists():
        raise FileNotFoundError(f"_root.toml not found in {plan_root}")

    root_meta = _load_root_meta(root_file)
    section_files: dict[str, tuple[Path, ...]] = {}
    for section in root_meta.sections:
        section_files[section] = _section_unit_files(plan_root, section)

    return PlanTree(
        plan_root=plan_root,
        root_meta=root_meta,
        section_files=section_files,
    )


def merge_tree(tree: PlanTree) -> FlatPlan:
    """Merge a PlanTree into a FlatPlan with path-qualified unit IDs.

    For each child TOML file, parse `[tasks.*]` tables. Each task's bare slug
    is validated against the slug grammar. The fully-qualified ID is
    `<section>/<file-stem>.<bare-slug>`.

    `depends_on` values are preserved as-is from source (resolver rewrites them).
    Within-file slug duplicates are impossible — tomllib rejects duplicate
    tables at parse time, before this function sees them.

    Raises:
        ValueError: invalid slug grammar, malformed task table, or bad field type.
    """
    units: dict[str, PlanUnit] = {}
    source_map: dict[str, Path] = {}
    root_file = tree.plan_root / "_root.toml"
    mtime_max = root_file.stat().st_mtime

    for section, toml_paths in tree.section_files.items():
        for toml_path in toml_paths:
            mtime_max = max(mtime_max, toml_path.stat().st_mtime)
            file_stem = toml_path.stem
            raw = tomllib.loads(toml_path.read_text())
            tasks = raw.get("tasks", {})
            for bare_slug, task_data in tasks.items():
                if not _SLUG_RE.match(bare_slug):
                    raise ValueError(
                        f"Invalid slug {bare_slug!r} in {toml_path}: must match {_SLUG_RE.pattern}"
                    )
                if not isinstance(task_data, dict):
                    raise ValueError(f"[tasks.{bare_slug}] must be a table in {toml_path}")
                fq_id = f"{section}/{file_stem}.{bare_slug}"
                units[fq_id] = _unit_from_task(fq_id, task_data, toml_path)
                source_map[fq_id] = toml_path

    return FlatPlan(
        plan_root=tree.plan_root,
        root_meta=tree.root_meta,
        units=units,
        source_map=source_map,
        source_mtime_max=mtime_max,
    )


def parse_compiled_source_mtime(value: str) -> float:
    """Parse serialized source_mtime_max metadata back to a timestamp."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Invalid source_mtime_max timestamp: {value!r}")


def resolve_refs(plan: FlatPlan) -> FlatPlan:
    """Rewrite each unit's `depends_on` to fully-qualified IDs.

    - Bare ref (no `/`) → resolved within the unit's own file scope.
    - Path-qualified ref (contains `/`) → looked up directly in the unit map.
    - Self-references are rejected.
    - Unknown refs raise with a `did you mean?` hint when possible.

    Returns a new FlatPlan with identical fields except `depends_on` values
    on each unit are now all fq_ids.
    """
    file_scope_to_ids: dict[str, set[str]] = {}
    for fq_id in plan.units:
        scope, _ = _split_fq_id(fq_id)
        file_scope_to_ids.setdefault(scope, set()).add(fq_id)

    resolved_units: dict[str, PlanUnit] = {}
    for fq_id, unit in plan.units.items():
        file_scope, _ = _split_fq_id(fq_id)
        resolved_deps: list[str] = []
        for ref in unit.depends_on:
            target = _resolve_ref(ref, fq_id, file_scope, plan.units, file_scope_to_ids)
            if target == fq_id:
                raise ValueError(f"Self-reference in {fq_id!r}: depends_on includes itself")
            resolved_deps.append(target)
        resolved_units[fq_id] = replace(unit, depends_on=tuple(resolved_deps))

    return replace(plan, units=resolved_units)


def _split_fq_id(fq_id: str) -> tuple[str, str]:
    """Split a path-qualified unit ID into `(file_scope, bare_slug)`.

    Bare slugs cannot contain `.` (enforced by merger), so `rpartition('.')`
    always splits correctly even if section or file stem contains `.`.
    """
    scope, _, bare = fq_id.rpartition(".")
    return scope, bare


def _resolve_ref(
    ref: str,
    from_unit: str,
    from_file_scope: str,
    all_units: dict[str, PlanUnit],
    file_scope_to_ids: dict[str, set[str]],
) -> str:
    """Resolve a single `depends_on` ref to a fq_id, or raise with a hint."""
    if "/" in ref:
        if ref in all_units:
            return ref
        hint = _closest_hint(ref, all_units.keys())
        raise ValueError(f"Unknown ref {ref!r} in depends_on of {from_unit!r}{hint}")

    # Bare slug — same-file scope
    target = f"{from_file_scope}.{ref}"
    if target in all_units:
        return target

    same_file_bares = {_split_fq_id(uid)[1] for uid in file_scope_to_ids.get(from_file_scope, ())}
    near = get_close_matches(ref, same_file_bares, n=1, cutoff=0.6)
    if near:
        hint = f"; did you mean {near[0]!r}?"
    else:
        cross_file = [uid for uid in all_units if _split_fq_id(uid)[1] == ref]
        hint = f"; did you mean {cross_file[0]!r}?" if len(cross_file) == 1 else ""
    raise ValueError(
        f"Unknown bare ref {ref!r} in depends_on of {from_unit!r}"
        f" (same-file scope: {from_file_scope}){hint}"
    )


def _closest_hint(ref: str, candidates: Any) -> str:
    matches = get_close_matches(ref, candidates, n=1, cutoff=0.6)
    return f"; did you mean {matches[0]!r}?" if matches else ""


def _str_list(value: Any, field: str, context: Path) -> tuple[str, ...]:
    """Coerce a TOML value to a tuple of strings, rejecting bad types."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} must be a list of strings in {context}")
    return tuple(value)


def _task_string(data: dict[str, Any], key: str, source: Path, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"[tasks.*].{key} must be a string in {source}")
    return value


def _task_int(
    data: dict[str, Any],
    key: str,
    source: Path,
    default: int | None = None,
) -> int | None:
    value = data.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"[tasks.*].{key} must be an integer in {source}")
    return value


def _task_int_default(data: dict[str, Any], key: str, source: Path, default: int) -> int:
    value = _task_int(data, key, source, default=default)
    return default if value is None else value


def _task_bool(data: dict[str, Any], key: str, source: Path, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"[tasks.*].{key} must be a boolean in {source}")
    return value


def _task_role(data: dict[str, Any], source: Path) -> Literal["worker", "researcher", "reviewer"]:
    value = _task_string(data, "role", source, default="worker")
    if value not in {"worker", "researcher", "reviewer"}:
        raise ValueError(
            f"[tasks.*].role must be one of: worker, researcher, reviewer in {source}"
        )
    return cast(Literal["worker", "researcher", "reviewer"], value)


def _unit_from_task(fq_id: str, data: dict[str, Any], source: Path) -> PlanUnit:
    """Build a PlanUnit from a parsed `[tasks.<slug>]` table."""
    files_data = data.get("files", {})
    if isinstance(files_data, list):
        # Flat shorthand: files = ["a.py", "b.py"] → touch list
        files = PlanUnitFiles(touch=_str_list(files_data, "files", source))
    elif isinstance(files_data, dict):
        files = PlanUnitFiles(
            create=_str_list(files_data.get("create", []), "files.create", source),
            edit=_str_list(files_data.get("edit", []), "files.edit", source),
            delete=_str_list(files_data.get("delete", []), "files.delete", source),
            read=_str_list(files_data.get("read", []), "files.read", source),
            touch=_str_list(files_data.get("touch", []), "files.touch", source),
        )
    else:
        raise ValueError(f"[tasks.*].files must be a list or table in {source}")
    return PlanUnit(
        slug=fq_id,
        summary=_task_string(data, "summary", source),
        prompt=_task_string(data, "prompt", source),
        commit_message=_task_string(data, "commit_message", source),
        files=files,
        depends_on=_str_list(data.get("depends_on", []), "depends_on", source),
        agent=_task_string(data, "agent", source),
        provider=_task_string(data, "provider", source),
        role=_task_role(data, source),
        timeout_s=_task_int_default(data, "timeout_s", source, default=0),
        iteration_budget=_task_int(data, "iteration_budget", source),
        test_cmd=_task_string(data, "test_cmd", source),
        prompt_file=_task_string(data, "prompt_file", source),
        sop_mapping=_str_list(data.get("sop_mapping", []), "sop_mapping", source),
        self_review=_task_bool(data, "self_review", source),
        max_fork_depth=_task_int_default(data, "max_fork_depth", source, default=1),
    )


@dataclass(frozen=True)
class ValidationReport:
    """Structural DAG validation results.

    `cycles` holds each non-trivial strongly connected component as a
    tuple of fq_ids sorted alphabetically. `unreachable` lists fq_ids
    with no execution-order path from any root (empty depends_on).
    Self-loops are rejected by `resolve_refs`, so single-node cycles
    only arise from malformed input that bypasses the resolver.
    """

    cycles: tuple[tuple[str, ...], ...]
    unreachable: tuple[str, ...]


@dataclass
class _CycleSearchState:
    index_counter: int = 0
    indices: dict[str, int] = field(default_factory=dict)
    lowlinks: dict[str, int] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    on_stack: set[str] = field(default_factory=set)
    components: list[tuple[str, ...]] = field(default_factory=list)


def validate(plan: FlatPlan) -> ValidationReport:
    """Run structural DAG checks on a resolved plan: cycles + unreachability.

    Returns the full report even if violations exist — callers decide what
    is fatal. File-claim conflicts are delegated to `plan.validate_plan`
    which runs against the compiled `_compiled.toml` output.
    """
    return ValidationReport(
        cycles=_find_cycles(plan.units),
        unreachable=_find_unreachable(plan.units),
    )


def _find_cycles(units: dict[str, PlanUnit]) -> tuple[tuple[str, ...], ...]:
    """Return every non-trivial SCC as a sorted fq_id tuple (Tarjan's)."""
    state = _CycleSearchState()
    for node in sorted(units):
        if node not in state.indices:
            _connect_cycle_node(units, state, node)
    return tuple(sorted(state.components))


def _connect_cycle_node(units: dict[str, PlanUnit], state: _CycleSearchState, node: str) -> None:
    _push_cycle_node(state, node)
    for dep in units[node].depends_on:
        _visit_cycle_dependency(units, state, node, dep)
    if state.lowlinks[node] == state.indices[node]:
        _record_cycle_component(units, state, node)


def _push_cycle_node(state: _CycleSearchState, node: str) -> None:
    state.indices[node] = state.index_counter
    state.lowlinks[node] = state.index_counter
    state.index_counter += 1
    state.stack.append(node)
    state.on_stack.add(node)


def _visit_cycle_dependency(
    units: dict[str, PlanUnit],
    state: _CycleSearchState,
    node: str,
    dep: str,
) -> None:
    if dep not in units:
        return
    if dep not in state.indices:
        _connect_cycle_node(units, state, dep)
        state.lowlinks[node] = min(state.lowlinks[node], state.lowlinks[dep])
    elif dep in state.on_stack:
        state.lowlinks[node] = min(state.lowlinks[node], state.indices[dep])


def _record_cycle_component(
    units: dict[str, PlanUnit],
    state: _CycleSearchState,
    node: str,
) -> None:
    component = _pop_cycle_component(state, node)
    if _is_cycle_component(units, component):
        state.components.append(tuple(sorted(component)))


def _pop_cycle_component(state: _CycleSearchState, node: str) -> list[str]:
    component: list[str] = []
    while True:
        item = state.stack.pop()
        state.on_stack.discard(item)
        component.append(item)
        if item == node:
            return component


def _is_cycle_component(units: dict[str, PlanUnit], component: list[str]) -> bool:
    return len(component) > 1 or component[0] in units[component[0]].depends_on


def _find_unreachable(units: dict[str, PlanUnit]) -> tuple[str, ...]:
    """Return fq_ids with no execution-order path from any root.

    Execution-order edges go prerequisite → dependent (reversed depends_on).
    Roots are units with empty `depends_on`; a unit is unreachable when no
    BFS/DFS traversal from any root touches it. Isolated cycles land here.
    """
    reverse_edges: dict[str, list[str]] = {uid: [] for uid in units}
    for uid, unit in units.items():
        for dep in unit.depends_on:
            if dep in reverse_edges:
                reverse_edges[dep].append(uid)

    visited: set[str] = set()
    stack = [uid for uid, unit in units.items() if not unit.depends_on]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(reverse_edges[node])
    return tuple(sorted(uid for uid in units if uid not in visited))
