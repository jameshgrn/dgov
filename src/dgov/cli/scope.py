"""Scope settlement preview CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.config import load_project_config
from dgov.plan import parse_plan_file
from dgov.project_root import resolve_project_root
from dgov.scope_status import ScopeStatus, analyze_scope_status, render_scope_status_lines


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

    files: set[str] = set()
    for line in status.stdout.rstrip("\n").split("\n"):
        if not line:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.add(path_part)

    return frozenset(files)


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

    config = load_project_config(project_root)
    scope_ignore_files = config.scope_ignore_files

    if plan is not None:
        from dgov.cli.plan import resolve_plan_input

        plan_file, _plan_dir = resolve_plan_input(plan)
        if not plan_file.exists():
            click.echo(f"Error: plan file not found: {plan_file}", err=True)
            raise click.exceptions.Exit(code=1)
        writable, read_only = _load_task_claims(plan_file, task)
        claimed_files = writable
        read_only_claims = read_only
    else:
        if not claim:
            click.echo("Error: --plan or at least one --claim is required", err=True)
            raise click.exceptions.Exit(code=1)
        claimed_files = list(claim)
        read_only_claims = list(read_files)

    actual_files = _get_actual_files(project_root)
    if actual_files is None:
        click.echo("Error: git status failed", err=True)
        raise click.exceptions.Exit(code=1)

    status = analyze_scope_status(
        actual_files=actual_files,
        claimed_files=claimed_files,
        read_files=read_only_claims,
        scope_ignore_files=scope_ignore_files,
        session_root=project_root,
        task_slug=task,
        pane_slug=pane,
    )

    if want_json():
        _render_scope_json(status)
    else:
        _render_scope_text(status)

    if status.blocking_failure:
        raise click.exceptions.Exit(code=1)
