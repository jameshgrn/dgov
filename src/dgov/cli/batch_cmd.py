"""Checkpoint and batch commands."""

from __future__ import annotations

import json
import os
import sys

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group()
def checkpoint():
    """Manage state checkpoints."""


@checkpoint.command("create")
@click.argument("name")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_create(name, project_root, session_root):
    """Create a named checkpoint of current state."""
    from dgov.batch import create_checkpoint

    result = create_checkpoint(project_root, name, session_root=session_root)
    click.echo(json.dumps(result, indent=2))


@checkpoint.command("list")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_list(project_root, session_root):
    """List all checkpoints."""
    from dgov.batch import list_checkpoints

    session_root = os.path.abspath(session_root or project_root)
    result = list_checkpoints(session_root)
    click.echo(json.dumps(result, indent=2))


@click.command("batch")
@click.argument("spec_path", type=click.Path(exists=True))
@SESSION_ROOT_OPTION
@click.option("--dry-run", is_flag=True, help="Show DAG tiers without executing")
def batch(spec_path, session_root, dry_run):
    """Execute a batch spec (TOML or JSON) with DAG-ordered parallelism.

    Tasks declare depends_on for explicit ordering and touches for implicit
    file-overlap serialization. On failure, transitive dependents are skipped.
    """
    from dgov.batch import run_batch

    result = run_batch(spec_path, session_root=session_root, dry_run=dry_run)
    if dry_run and result.get("ascii_dag"):
        click.echo(result["ascii_dag"])
    else:
        click.echo(json.dumps(result, indent=2))
    if result.get("failed"):
        sys.exit(1)
