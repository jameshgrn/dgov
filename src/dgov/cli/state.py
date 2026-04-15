"""Manual task-state repair commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from dgov.cli import cli
from dgov.cli.run import _cmd_run_plan
from dgov.deploy_log import append as deploy_append, is_deployed
from dgov.persistence import add_task, emit_event, get_task, reset_task_state
from dgov.persistence.schema import TaskState, WorkerTask
from dgov.plan import PlanSpec, parse_plan_file
from dgov.project_root import resolve_project_root
from dgov.types import Worktree
from dgov.worktree import remove_worktree

_RETRYABLE_STATES = frozenset({
    TaskState.FAILED,
    TaskState.ABANDONED,
    TaskState.TIMED_OUT,
    TaskState.SKIPPED,
})


def _git_head_sha(project_root: Path) -> str:
    """Return HEAD sha for manual deploy bookkeeping."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _resolve_plan_for_slug(
    project_root: Path, slug: str, plan_name: str | None = None
) -> tuple[Path, PlanSpec]:
    """Resolve a unique compiled plan containing slug."""
    plans_root = project_root / ".dgov" / "plans"
    candidates: list[Path] = []

    if plan_name:
        candidate = plans_root / plan_name / "_compiled.toml"
        if candidate.exists():
            candidates.append(candidate)

    if not candidates and plans_root.exists():
        for candidate in sorted(plans_root.glob("*")):
            if candidate.name == "archive" or not candidate.is_dir():
                continue
            compiled = candidate / "_compiled.toml"
            if compiled.exists():
                candidates.append(compiled)

    matches: list[tuple[Path, PlanSpec]] = []
    for compiled in candidates:
        try:
            plan = parse_plan_file(str(compiled))
        except Exception:
            continue
        if slug in plan.units:
            matches.append((compiled, plan))

    if not matches:
        raise click.ClickException(f"Task '{slug}' not found in any compiled plan.")
    if len(matches) > 1:
        plans = ", ".join(plan.name for _, plan in matches)
        raise click.ClickException(
            f"Task '{slug}' is ambiguous across compiled plans: {plans}. "
            "Restore state.db or prune old plans."
        )
    return matches[0]


def _cleanup_task_worktree(project_root: Path, task: dict) -> None:
    """Remove any preserved worktree before resetting task state."""
    worktree_path = task.get("worktree_path")
    branch_name = task.get("branch_name")
    if not worktree_path or not branch_name:
        return
    try:
        remove_worktree(
            str(project_root),
            Worktree(path=Path(str(worktree_path)), branch=str(branch_name), commit=""),
        )
    except Exception:
        click.echo(f"Warning: could not remove worktree {worktree_path}", err=True)


@cli.command(name="retry")
@click.argument("slug")
def retry_cmd(slug: str) -> None:
    """Reset a failed task and rerun just that unit plus its dependencies."""
    project_root = resolve_project_root()
    task = get_task(str(project_root), slug)
    if task is None:
        raise click.ClickException(
            f"Task '{slug}' not found in state.db. If state was lost, "
            f"use `dgov mark-done {slug}` or rerun the plan."
        )
    state = task.get("state", "")
    if state not in _RETRYABLE_STATES:
        raise click.ClickException(
            f"Task '{slug}' is in state '{state}', not retryable. "
            f"Expected one of: {sorted(s.value for s in _RETRYABLE_STATES)}."
        )

    plan_name = str(task.get("plan_name") or "")
    if not plan_name:
        raise click.ClickException(f"Task '{slug}' has no plan_name in state.db.")

    _cleanup_task_worktree(project_root, task)
    reset_task_state(str(project_root), slug, plan_name=plan_name)
    compiled_path, _ = _resolve_plan_for_slug(project_root, slug, plan_name=plan_name)
    click.echo(f"Retrying {slug} from {compiled_path.parent.name}")
    _cmd_run_plan(
        str(compiled_path),
        str(project_root),
        only=slug,
        plan_dir=compiled_path.parent,
    )


@cli.command(name="mark-done")
@click.argument("slug")
def mark_done_cmd(slug: str) -> None:
    """Mark a plan unit as already merged in the current checkout."""
    project_root = resolve_project_root()
    task_row = get_task(str(project_root), slug)
    hinted_plan = str(task_row.get("plan_name") or "") if task_row else None
    compiled_path, plan = _resolve_plan_for_slug(project_root, slug, plan_name=hinted_plan)
    unit = plan.units[slug]
    head_sha = _git_head_sha(project_root)

    reset_task_state(str(project_root), slug, plan_name=plan.name)
    file_claims = tuple(
        dict.fromkeys(unit.files.create + unit.files.edit + unit.files.delete + unit.files.touch)
    )
    add_task(
        str(project_root),
        WorkerTask(
            slug=slug,
            prompt=unit.prompt or f"manually marked done: {slug}",
            agent=unit.agent or "manual",
            project_root=str(project_root),
            worktree_path="",
            branch_name="",
            role=unit.role,
            state=TaskState.MERGED,
            plan_name=plan.name,
            file_claims=file_claims,
            commit_message=unit.commit_message or None,
        ),
    )

    pane = f"manual-{slug}"
    emit_event(
        str(project_root),
        "dag_task_dispatched",
        pane,
        plan_name=plan.name,
        task_slug=slug,
        agent=unit.agent or "manual",
    )
    emit_event(
        str(project_root),
        "task_done",
        pane,
        plan_name=plan.name,
        task_slug=slug,
    )
    emit_event(
        str(project_root),
        "review_pass",
        pane,
        plan_name=plan.name,
        task_slug=slug,
        commit_count=1,
    )
    emit_event(
        str(project_root),
        "merge_completed",
        pane,
        plan_name=plan.name,
        task_slug=slug,
        merge_sha=head_sha,
    )

    if not is_deployed(str(project_root), plan.name, slug):
        deploy_append(str(project_root), plan.name, slug, head_sha)

    click.echo(f"Marked {slug} done in {compiled_path.parent.name} at {head_sha[:7]}")
