"""Completed-run lifecycle maintenance for `dgov run`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

from dgov.archive import archive_plan
from dgov.cli import run_checks, run_git, run_output, want_json
from dgov.deploy_log import is_plan_complete


def refresh_sentrux_baseline_after_clean_run(
    project_root: str,
    *,
    run_sentrux: run_checks.SentruxRunner,
) -> None:
    root = Path(project_root).resolve()
    root_str = str(root)
    if not want_json():
        click.echo("[sentrux] Refreshing accepted baseline after clean run...")

    committed = run_checks.refresh_accepted_sentrux_baseline(root_str, run_sentrux=run_sentrux)

    if not want_json():
        status = "committed" if committed else "already current"
        click.echo(f"[sentrux] Accepted baseline refreshed ({status}).")


def should_refresh_sentrux_baseline(
    *,
    run_status: str,
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    only: str | None,
    plan_dir: Path | None,
    project_root: str,
    plan_name: str,
    task_slugs: set[str],
) -> bool:
    root = str(Path(project_root).resolve())
    return (
        run_status == "complete"
        and only is None
        and plan_dir is not None
        and not run_output.sentrux_failed(gate_result)
        and not run_output.branch_verification_failed(branch_result)
        and is_plan_complete(root, plan_name, task_slugs)
    )


def maybe_refresh_sentrux_baseline(
    *,
    run_status: str,
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    only: str | None,
    plan_dir: Path | None,
    project_root: str,
    plan_name: str,
    task_slugs: set[str],
    run_sentrux: run_checks.SentruxRunner,
) -> None:
    root = str(Path(project_root).resolve())
    if not should_refresh_sentrux_baseline(
        run_status=run_status,
        gate_result=gate_result,
        branch_result=branch_result,
        only=only,
        plan_dir=plan_dir,
        project_root=root,
        plan_name=plan_name,
        task_slugs=task_slugs,
    ):
        return
    refresh_sentrux_baseline_after_clean_run(root, run_sentrux=run_sentrux)


def maybe_archive_completed_plan(
    *,
    run_status: str,
    only: str | None,
    plan_dir: Path | None,
    project_root: str,
    plan_name: str,
    task_slugs: set[str],
    archive: Callable[[Path], Path] | None = None,
) -> None:
    root = str(Path(project_root).resolve())
    if (
        run_status != "complete"
        or only is not None
        or plan_dir is None
        or not is_plan_complete(root, plan_name, task_slugs)
    ):
        return
    archive_fn = archive or archive_plan
    dest = archive_fn(plan_dir)
    if not want_json():
        click.echo(f"Plan fully deployed → archived to {dest}")
        run_git.warn_if_archive_left_git_changes(root)
