"""Scope settlement preview CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import click

from dgov.cli import cli, load_project_config_or_exit, want_json
from dgov.git_status import porcelain_status_paths
from dgov.plan import parse_plan_file
from dgov.project_root import resolve_project_root
from dgov.scope_status import ScopeStatus, analyze_scope_status, render_scope_status_lines


@dataclass(frozen=True)
class _ScopeClaims:
    writable: list[str]
    read_only: list[str]


def _get_actual_files(project_root: str) -> frozenset[str] | None:
    """Collect actual modified files from git status --porcelain."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return None

    return frozenset(porcelain_status_paths(status.stdout, include_rename_sources=True))


def _load_task_claims(plan_path: Path, task_slug: str) -> tuple[list[str], list[str]]:
    """Load writable and read-only file claims for a task from a compiled plan."""
    plan = parse_plan_file(str(plan_path))
    unit = plan.units.get(task_slug)
    if unit is None:
        raise click.ClickException(f"Task '{task_slug}' not found in plan")

    writable: list[str] = []
    for kind in ("create", "edit", "delete", "touch"):
        files = getattr(unit.files, kind)
        if files:
            writable.extend(files)

    read_only = list(unit.files.read) if unit.files.read else []
    return writable, read_only


def _render_scope_text(status: ScopeStatus) -> None:
    """Render scope status as human-readable text."""
    for line in render_scope_status_lines(status):
        if status.blocking_failure and line.startswith("blocking: "):
            click.echo(click.style(line, fg="red"), err=True)
        else:
            click.echo(line)


def _render_scope_json(status: ScopeStatus) -> None:
    """Render scope status as JSON."""
    payload = {
        "claimed_writable": sorted(status.claimed_writable),
        "claimed_readonly": sorted(status.claimed_readonly),
        "modified_files": sorted(status.actual_files),
        "transient_write_paths": sorted(status.transient_write_paths),
        "ignored_actual_paths": sorted(status.ignored_actual_paths),
        "ignored_transient_paths": sorted(status.ignored_transient_paths),
        "unclaimed_actual_paths": sorted(status.unclaimed_actual_paths),
        "unclaimed_transient_paths": sorted(status.unclaimed_transient_paths),
        "blocking_failure": (
            {
                "passed": status.blocking_failure.passed,
                "verdict": status.blocking_failure.verdict,
                "error": status.blocking_failure.error,
            }
            if status.blocking_failure
            else None
        ),
    }
    click.echo(json.dumps(payload, indent=2))


def _resolve_scope_claims(
    *,
    plan: Path | None,
    task: str,
    claim: tuple[str, ...],
    read_files: tuple[str, ...],
) -> _ScopeClaims:
    if plan is not None:
        return _resolve_plan_scope_claims(plan, task)
    if not claim:
        click.echo("Error: --plan or at least one --claim is required", err=True)
        raise click.exceptions.Exit(code=1)
    return _ScopeClaims(writable=list(claim), read_only=list(read_files))


def _resolve_plan_scope_claims(plan: Path, task: str) -> _ScopeClaims:
    from dgov.cli.plan import resolve_plan_input

    plan_file, _plan_dir = resolve_plan_input(plan)
    if not plan_file.exists():
        click.echo(f"Error: plan file not found: {plan_file}", err=True)
        raise click.exceptions.Exit(code=1)
    writable, read_only = _load_task_claims(plan_file, task)
    return _ScopeClaims(writable=writable, read_only=read_only)


def _analyze_scope_status_for_cli(
    *,
    project_root: str,
    task: str,
    pane: str | None,
    claims: _ScopeClaims,
    scope_ignore_files: Sequence[str],
) -> ScopeStatus:
    actual_files = _get_actual_files(project_root)
    if actual_files is None:
        click.echo("Error: git status failed", err=True)
        raise click.exceptions.Exit(code=1)

    return analyze_scope_status(
        actual_files=actual_files,
        claimed_files=claims.writable,
        read_files=claims.read_only,
        scope_ignore_files=scope_ignore_files,
        session_root=project_root,
        task_slug=task,
        pane_slug=pane,
    )


@cli.group(name="scope")
def scope_cmd() -> None:
    """Scope settlement preview."""
    pass


@scope_cmd.command(name="status")
@click.option("--task", required=True, help="Task slug to inspect")
@click.option(
    "--plan",
    type=click.Path(path_type=Path, exists=True),
    help="Compiled plan file or plan directory",
)
@click.option("--pane", help="Pane slug for transient write filtering")
@click.option("--claim", multiple=True, help="Explicit writable file claim (ad hoc mode)")
@click.option(
    "--read",
    "read_files",
    multiple=True,
    help="Explicit read-only file claim (ad hoc mode)",
)
def scope_status_cmd(
    task: str,
    plan: Path | None,
    pane: str | None,
    claim: tuple[str, ...],
    read_files: tuple[str, ...],
) -> None:
    """Preview settlement scope status for a task.

    When --plan is given, file claims are loaded from the compiled plan for the
    specified --task. Without --plan, use --claim and --read for ad hoc inspection.

    \b
    Example: dgov scope status --task tasks/main.a --plan .dgov/plans/my-plan/
    Example: dgov scope status --task my-task --claim src/a.py --read tests/test_a.py
    """
    project_root = str(resolve_project_root())

    config = load_project_config_or_exit(project_root)
    claims = _resolve_scope_claims(
        plan=plan,
        task=task,
        claim=claim,
        read_files=read_files,
    )
    status = _analyze_scope_status_for_cli(
        project_root=project_root,
        task=task,
        pane=pane,
        claims=claims,
        scope_ignore_files=config.scope_ignore_files,
    )

    if want_json():
        _render_scope_json(status)
    else:
        _render_scope_text(status)

    if status.blocking_failure:
        raise click.exceptions.Exit(code=1)
