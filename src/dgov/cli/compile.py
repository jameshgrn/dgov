"""Compile subcommand — plan tree compilation pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, print_dag_graph, want_json
from dgov.project_root import resolve_project_root


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
    _cmd_compile(plan_root, dry_run=dry_run, recompile_sops=recompile_sops, graph=graph)


def _cmd_compile(plan_root: Path, *, dry_run: bool, recompile_sops: bool, graph: bool) -> None:
    """Compile pipeline: walk → merge → resolve → validate → bundle → write."""
    from dgov.config import load_project_config
    from dgov.plan_tree import (
        merge_tree,
        resolve_refs,
        validate,
        walk_tree,
    )
    from dgov.serializer import serialize_compiled_toml
    from dgov.sop_bundler import IdentityBundler, TagBasedSopBundler, bundle

    # 1. Walk
    try:
        tree = walk_tree(plan_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 2. Merge
    try:
        flat = merge_tree(tree)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None

    if not flat.units:
        click.echo("Error: plan tree has no units", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 3. Resolve
    try:
        resolved = resolve_refs(flat)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 4. Validate
    report = validate(resolved)
    if report.cycles or report.unreachable:
        if report.cycles:
            for cycle in report.cycles:
                click.echo(f"  ERROR: cycle: {' → '.join(cycle)}", err=True)
        if report.unreachable:
            click.echo(f"  ERROR: unreachable: {', '.join(report.unreachable)}", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 5. Bundle SOPs
    project_root = resolve_project_root()
    project_config = load_project_config(project_root)
    sops_dir = project_root / ".dgov" / "sops"
    bundler = IdentityBundler() if dry_run else TagBasedSopBundler()

    # Caching: read existing _compiled.toml if present to reuse mapping if hash matches
    cached_mapping = None
    cached_hash = None
    compiled_path = plan_root / "_compiled.toml"
    if compiled_path.exists() and not recompile_sops:
        try:
            from dgov.dag_parser import parse_dag_file

            old = parse_dag_file(str(compiled_path))
            cached_hash = old.sop_set_hash
            cached_mapping = {uid: task.sop_mapping for uid, task in old.tasks.items()}
        except Exception:
            pass  # Silently skip cache on parse failure

    try:
        result = bundle(
            resolved,
            sops_dir,
            bundler,
            cached_mapping=cached_mapping,
            cached_hash=cached_hash,
        )
    except Exception as e:
        click.echo(f"  ERROR: {e!s}", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 6. Serialize + write
    toml_str = serialize_compiled_toml(result, resolved.source_mtime_max)
    out_path = plan_root / "_compiled.toml"
    out_path.write_text(toml_str)

    # 7a. Validate compiled plan — surface unclaimed test file warnings
    from dgov.plan import parse_plan_file, validate_plan

    compiled_spec = parse_plan_file(str(out_path))
    plan_issues = validate_plan(compiled_spec, departments=project_config.departments)
    plan_errors = [i for i in plan_issues if i.severity == "error"]
    plan_warnings = [i for i in plan_issues if i.severity == "warning"]

    if plan_errors:
        out_path.unlink(missing_ok=True)
        for issue in plan_errors:
            unit_info = f" [{issue.unit}]" if issue.unit else ""
            click.echo(f"  ERROR{unit_info}: {issue.message}", err=True)
        raise click.exceptions.Exit(code=1) from None

    # 7. Summary
    edge_count = sum(len(u.depends_on) for u in resolved.units.values())
    sop_count = sum(1 for m in result.sop_mapping.values() if m)

    if want_json():
        click.echo(
            json.dumps(
                {
                    "status": "compiled",
                    "output": str(out_path),
                    "units": len(resolved.units),
                    "edges": edge_count,
                    "sops_assigned": sop_count,
                    "sop_set_hash": result.sop_set_hash,
                    "dry_run": dry_run,
                    "warnings": [{"unit": w.unit, "message": w.message} for w in plan_warnings],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Compiled {len(resolved.units)} units, {edge_count} edges → {out_path}")
        if sop_count:
            click.echo(f"  SOPs assigned to {sop_count} unit(s)")
        if dry_run:
            click.echo("  (dry-run: identity bundler, no LLM call)")
        for w in plan_warnings:
            unit_info = f" [{w.unit}]" if w.unit else ""
            click.echo(f"  WARNING{unit_info}: {w.message}", err=True)

    if graph and not want_json():
        print_dag_graph(resolved.units)
