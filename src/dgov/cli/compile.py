"""Compile subcommand — plan tree compilation pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from dgov.cli import cli, print_dag_graph, want_json
from dgov.project_root import resolve_project_root

if TYPE_CHECKING:
    from dgov.config import ProjectConfig
    from dgov.plan import PlanIssue
    from dgov.plan_tree import FlatPlan, PlanTree
    from dgov.sop_bundler import BundleResult, SopBundler


@dataclass(frozen=True)
class _SopCache:
    mapping: dict[str, tuple[str, ...]] | None = None
    sop_set_hash: str | None = None


@dataclass(frozen=True)
class _CompileSummary:
    output_path: Path
    unit_count: int
    edge_count: int
    sop_count: int
    sop_set_hash: str
    dry_run: bool
    warnings: tuple[PlanIssue, ...]


@cli.command(name="compile")
@click.argument("plan_root", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--dry-run", is_flag=True, help="Use identity SOP bundler (no LLM call)")
@click.option(
    "--recompile-sops", is_flag=True, help="Force SOP re-assignment even if hash matches"
)
@click.option("--graph", is_flag=True, help="Print DAG shape after compile")
def compile_cmd(plan_root: Path, dry_run: bool, recompile_sops: bool, graph: bool) -> None:
    """Compile a plan tree into _compiled.toml.

    Walks the plan tree, merges units, resolves refs, validates the DAG,
    bundles SOPs, and writes a dispatch-ready _compiled.toml.

    \b
    Example: dgov compile .dgov/plans/my-plan/
    """
    compile_plan_dir(plan_root, dry_run=dry_run, recompile_sops=recompile_sops, graph=graph)


def compile_plan_dir(plan_root: Path, *, dry_run: bool, recompile_sops: bool, graph: bool) -> None:
    """Compile pipeline: walk → merge → resolve → validate → bundle → write."""
    resolved = _load_resolved_plan(plan_root)
    project_root = resolve_project_root()
    project_config = _load_project_config(project_root)
    bundle_result = _bundle_sops(
        resolved,
        plan_root,
        project_root=project_root,
        dry_run=dry_run,
        recompile_sops=recompile_sops,
    )
    out_path = _write_compiled_plan(plan_root, bundle_result, resolved)
    warnings = _validate_compiled_plan(out_path, project_config)
    summary = _build_summary(out_path, resolved, bundle_result, dry_run, warnings)

    _print_summary(summary)
    _print_graph_if_requested(graph, resolved)


def _load_resolved_plan(plan_root: Path) -> FlatPlan:
    tree = _walk_plan_tree(plan_root)
    flat = _merge_plan_tree(tree)
    _ensure_plan_has_units(flat)
    resolved = _resolve_plan_refs(flat)
    _validate_dag(resolved)
    return resolved


def _walk_plan_tree(plan_root: Path) -> PlanTree:
    from dgov.plan_tree import walk_tree

    try:
        return walk_tree(plan_root)
    except (FileNotFoundError, ValueError) as exc:
        _exit_with_error(f"Error: {exc}")


def _merge_plan_tree(tree: PlanTree) -> FlatPlan:
    from dgov.plan_tree import merge_tree

    try:
        return merge_tree(tree)
    except ValueError as exc:
        _exit_with_error(f"Error: {exc}")


def _ensure_plan_has_units(plan: FlatPlan) -> None:
    if not plan.units:
        _exit_with_error("Error: plan tree has no units")


def _resolve_plan_refs(plan: FlatPlan) -> FlatPlan:
    from dgov.plan_tree import resolve_refs

    try:
        return resolve_refs(plan)
    except ValueError as exc:
        _exit_with_error(f"Error: {exc}")


def _validate_dag(plan: FlatPlan) -> None:
    from dgov.plan_tree import validate

    report = validate(plan)
    if not (report.cycles or report.unreachable):
        return

    for cycle in report.cycles:
        click.echo(f"  ERROR: cycle: {' → '.join(cycle)}", err=True)
    if report.unreachable:
        click.echo(f"  ERROR: unreachable: {', '.join(report.unreachable)}", err=True)
    raise click.exceptions.Exit(code=1) from None


def _load_project_config(project_root: Path) -> ProjectConfig:
    from dgov.config import load_project_config

    return load_project_config(project_root)


def _bundle_sops(
    plan: FlatPlan,
    plan_root: Path,
    *,
    project_root: Path,
    dry_run: bool,
    recompile_sops: bool,
) -> BundleResult:
    sops_dir = project_root / ".dgov" / "sops"
    bundler = _select_sop_bundler(dry_run)
    cache = _load_sop_cache(plan_root / "_compiled.toml", recompile_sops=recompile_sops)
    result = _run_sop_bundle(plan, sops_dir, bundler, cache)

    if dry_run:
        result = replace(result, sop_set_hash="")
    _print_sop_assignments(result, dry_run=dry_run)
    return result


def _select_sop_bundler(dry_run: bool) -> SopBundler:
    from dgov.sop_bundler import IdentityBundler, TagBasedSopBundler

    return IdentityBundler() if dry_run else TagBasedSopBundler()


def _load_sop_cache(compiled_path: Path, *, recompile_sops: bool) -> _SopCache:
    if recompile_sops or not compiled_path.exists():
        return _SopCache()

    try:
        from dgov.dag_parser import parse_dag_file

        old = parse_dag_file(str(compiled_path))
    except Exception:
        return _SopCache()

    return _SopCache(
        mapping={uid: task.sop_mapping for uid, task in old.tasks.items()},
        sop_set_hash=old.sop_set_hash,
    )


def _run_sop_bundle(
    plan: FlatPlan,
    sops_dir: Path,
    bundler: SopBundler,
    cache: _SopCache,
) -> BundleResult:
    from dgov.sop_bundler import bundle

    try:
        return bundle(
            plan,
            sops_dir,
            bundler,
            cached_mapping=cache.mapping,
            cached_hash=cache.sop_set_hash,
        )
    except Exception as exc:
        _exit_with_error(f"  ERROR: {exc!s}")


def _print_sop_assignments(result: BundleResult, *, dry_run: bool) -> None:
    if dry_run:
        return

    for uid, names in sorted(result.sop_mapping.items()):
        if names:
            click.echo(f"  SOPs: {uid.split('.')[-1]} → {', '.join(names)}", err=True)
    no_sop_units = [uid for uid, names in result.sop_mapping.items() if not names]
    if no_sop_units:
        click.echo(
            f"  WARNING: {len(no_sop_units)} unit(s) matched zero SOPs",
            err=True,
        )


def _write_compiled_plan(
    plan_root: Path,
    result: BundleResult,
    resolved: FlatPlan,
) -> Path:
    from dgov.serializer import serialize_compiled_toml

    toml_str = serialize_compiled_toml(result, resolved.source_mtime_max)
    out_path = plan_root / "_compiled.toml"
    out_path.write_text(toml_str)
    return out_path


def _validate_compiled_plan(
    out_path: Path,
    project_config: ProjectConfig,
) -> tuple[PlanIssue, ...]:
    from dgov.plan import parse_plan_file, validate_plan

    compiled_spec = parse_plan_file(str(out_path))
    plan_issues = validate_plan(compiled_spec, departments=project_config.departments)
    plan_errors = tuple(i for i in plan_issues if i.severity == "error")
    plan_warnings = tuple(i for i in plan_issues if i.severity == "warning")

    if plan_errors:
        out_path.unlink(missing_ok=True)
        _print_plan_errors(plan_errors)
        raise click.exceptions.Exit(code=1) from None

    return plan_warnings


def _print_plan_errors(plan_errors: tuple[PlanIssue, ...]) -> None:
    for issue in plan_errors:
        unit_info = f" [{issue.unit}]" if issue.unit else ""
        click.echo(f"  ERROR{unit_info}: {issue.message}", err=True)


def _build_summary(
    out_path: Path,
    resolved: FlatPlan,
    result: BundleResult,
    dry_run: bool,
    warnings: tuple[PlanIssue, ...],
) -> _CompileSummary:
    return _CompileSummary(
        output_path=out_path,
        unit_count=len(resolved.units),
        edge_count=sum(len(u.depends_on) for u in resolved.units.values()),
        sop_count=sum(1 for mapping in result.sop_mapping.values() if mapping),
        sop_set_hash=result.sop_set_hash,
        dry_run=dry_run,
        warnings=warnings,
    )


def _print_summary(summary: _CompileSummary) -> None:
    if want_json():
        _print_json_summary(summary)
        return

    _print_text_summary(summary)


def _print_json_summary(summary: _CompileSummary) -> None:
    click.echo(
        json.dumps(
            {
                "status": "compiled",
                "output": str(summary.output_path),
                "units": summary.unit_count,
                "edges": summary.edge_count,
                "sops_assigned": summary.sop_count,
                "sop_set_hash": summary.sop_set_hash,
                "dry_run": summary.dry_run,
                "warnings": [{"unit": w.unit, "message": w.message} for w in summary.warnings],
            },
            indent=2,
        )
    )


def _print_text_summary(summary: _CompileSummary) -> None:
    click.echo(
        f"Compiled {summary.unit_count} units, {summary.edge_count} edges → {summary.output_path}"
    )
    if summary.sop_count:
        click.echo(f"  SOPs assigned to {summary.sop_count} unit(s)")
    if summary.dry_run:
        click.echo("  (dry-run: identity bundler, no LLM call)")
    for warning in summary.warnings:
        unit_info = f" [{warning.unit}]" if warning.unit else ""
        click.echo(f"  WARNING{unit_info}: {warning.message}", err=True)


def _print_graph_if_requested(graph: bool, resolved: FlatPlan) -> None:
    if graph and not want_json():
        print_dag_graph(resolved.units)


def _exit_with_error(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise click.exceptions.Exit(code=1) from None
