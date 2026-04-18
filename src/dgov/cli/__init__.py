"""dgov CLI — headless governor surface."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import click

from dgov import __version__
from dgov.persistence import prune_history, read_events
from dgov.project_root import resolve_project_root
from dgov.types import TaskState

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dgov")


def want_json() -> bool:
    """Check if JSON output is requested via env var or context."""
    return os.environ.get("DGOV_JSON", "").strip() in ("1", "true", "yes")


def _output(data: dict) -> None:
    """Output data as JSON or human-readable."""
    if want_json():
        click.echo(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            click.echo(f"{key}: {value}")


def resolve_plan_input(path: Path) -> tuple[Path, Path | None]:
    """Accept either a plan directory or a compiled TOML file.

    Returns (plan_file, plan_dir) where plan_file is the TOML path to load
    (may or may not exist — callers that need richer error messages check
    existence themselves) and plan_dir is the directory when the caller
    passed one or None when the caller passed a bare file.

    Raises click.ClickException only for clearly invalid inputs (non-TOML
    file path). Missing _compiled.toml is a caller-level concern.
    """
    if path.is_dir():
        return path / "_compiled.toml", path
    if path.suffix != ".toml":
        raise click.ClickException(f"Plan file must be .toml, got: {path}")
    return path, None


def print_dag_graph(units: dict) -> None:
    """Print an ASCII representation of a plan DAG.

    Works on any mapping of slug → object with a `depends_on` tuple,
    so it can print both FlatPlan (from plan_tree) and PlanSpec (from plan).
    """
    children: dict[str, set[str]] = {uid: set() for uid in units}
    for uid, unit in units.items():
        for dep in getattr(unit, "depends_on", ()):
            if dep in children:
                children[dep].add(uid)

    roots = sorted(uid for uid, unit in units.items() if not getattr(unit, "depends_on", ()))
    edge_count = sum(len(getattr(u, "depends_on", ())) for u in units.values())
    click.echo(f"\nDAG ({len(units)} tasks, {edge_count} edges):")

    if not units:
        click.echo("  (empty)")
        return

    visited: set[str] = set()

    def _walk(uid: str, prefix: str, is_last: bool) -> None:
        if uid in visited:
            connector = "    └─► " if is_last else "    ├─► "
            click.echo(f"{prefix}{connector}{uid} ...")
            return
        visited.add(uid)
        is_root = uid in roots
        label = f"{uid} (root)" if is_root else uid
        if not prefix:
            click.echo(f"  {label}")
        else:
            connector = "└─► " if is_last else "├─► "
            click.echo(f"{prefix}{connector}{label}")
        child_ids = sorted(children.get(uid, set()))
        for i, child_id in enumerate(child_ids):
            is_last_child = i == len(child_ids) - 1
            extension = "    " if is_last else "│   "
            _walk(child_id, prefix + extension, is_last_child)

    for root in roots:
        _walk(root, "", True)


@click.group(invoke_without_command=True)
@click.option("--json", is_flag=True, help="Output as JSON")
@click.version_option(version=__version__, prog_name="dgov")
@click.pass_context
def cli(
    ctx: click.Context,
    json: bool,
) -> None:
    """dgov — headless governor.

    \b
    USAGE:
      dgov                       Show status
      dgov preflight             Run settlement gates on local changes
      dgov run <dir>             Compile and run a plan directory
      dgov compile <dir>         Compile plan tree to _compiled.toml
      dgov init                  Bootstrap .dgov/project.toml and governor.md
      dgov init-plan <name>      Initialize a new plan directory
      dgov fix <prompt>          Create and run a one-off fix plan
      dgov watch                 Stream events live
      dgov archive-plan <name>   Manually archive a plan
      dgov plan status <dir>     Show pending vs deployed units
      dgov plan review <dir>     Post-hoc debrief of the last run
      dgov sentrux check         Run architectural quality check

    Tasks run in isolated git worktrees. No tmux required.
    """
    if json:
        os.environ["DGOV_JSON"] = "1"

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` → show status
    _cmd_status(str(resolve_project_root()))


@cli.command(name="status")
@click.option(
    "--all", "show_all", is_flag=True, help="Show event-derived history, not just live tasks"
)
def status_cmd(show_all: bool) -> None:
    """Show governor status — what's running now."""
    _cmd_status(str(resolve_project_root()), show_all=show_all)


@cli.command(name="archive-plan")
@click.argument("name")
def archive_plan_cmd(name: str) -> None:
    """Manually archive a plan directory to .dgov/plans/archive/<name>."""
    from dgov.archive import archive_plan

    project_root = resolve_project_root()
    plan_dir = project_root / ".dgov" / "plans" / name
    if not plan_dir.exists():
        click.echo(f"Error: Plan not found: {plan_dir}", err=True)
        raise click.exceptions.Exit(code=1)
    archive_dir = plan_dir.parent / "archive"
    if (archive_dir / name).exists():
        click.echo(f"Error: Archive already exists: {archive_dir / name}", err=True)
        raise click.exceptions.Exit(code=1)
    dest = archive_plan(plan_dir)
    click.echo(f"Archived to {dest}")


@cli.command(name="prune")
def prune_cmd() -> None:
    """Prune historical tasks — removes abandoned and closed records."""
    project_root = str(resolve_project_root())
    try:
        count = prune_history(project_root)
        if count == 0:
            click.echo("Nothing to prune.")
        else:
            click.echo(f"Pruned {count} historical task(s).")
    except Exception as exc:
        click.echo(f"Prune failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc


# States that represent in-flight governor work, not persisted history.
_LIVE_STATES = frozenset({
    TaskState.PENDING.value,
    TaskState.ACTIVE.value,
    TaskState.DONE.value,
    TaskState.REVIEWING.value,
    TaskState.REVIEWED_PASS.value,
    TaskState.REVIEWED_FAIL.value,
    TaskState.MERGING.value,
})


def _status_state_from_event(event: dict) -> str | None:
    """Map a lifecycle event to the task state it establishes."""
    event_name = event.get("event")
    if event_name == "dag_task_dispatched":
        return TaskState.ACTIVE.value
    if event_name == "task_done":
        return TaskState.DONE.value
    if event_name == "task_abandoned":
        return TaskState.ABANDONED.value
    if event_name == "task_timed_out":
        return TaskState.TIMED_OUT.value
    if event_name == "task_failed":
        error = str(event.get("error", "")).lower()
        if "timeout" in error:
            return TaskState.TIMED_OUT.value
        return TaskState.FAILED.value
    if event_name == "review_pass":
        return TaskState.REVIEWED_PASS.value
    if event_name == "review_fail":
        return TaskState.REVIEWED_FAIL.value
    if event_name == "merge_completed":
        return TaskState.MERGED.value
    if event_name == "task_merge_failed":
        return TaskState.FAILED.value
    if event_name == "task_closed":
        return TaskState.CLOSED.value
    if event_name == "dag_task_governor_resumed":
        action = event.get("action")
        if action == "retry":
            return TaskState.PENDING.value
        if action == "skip":
            return TaskState.SKIPPED.value
        if action == "fail":
            return TaskState.FAILED.value
    return None


def _latest_run_start_ids(events: list[dict]) -> dict[str, int]:
    """Return the latest run_start id for each plan present in the event log."""
    latest: dict[str, int] = {}
    for event in events:
        if event.get("event") != "run_start":
            continue
        plan_name = event.get("plan_name")
        if not plan_name:
            continue
        latest[plan_name] = max(latest.get(plan_name, 0), int(event.get("id", 0)))
    return latest


def _status_tasks_from_events(project_root: str, *, latest_run_only: bool) -> list[dict[str, str]]:
    """Build task snapshots from lifecycle events instead of mutable task rows."""
    events = read_events(project_root)
    if not events:
        return []

    latest_run_ids = _latest_run_start_ids(events) if latest_run_only else {}
    task_statuses: dict[tuple[str, str], dict[str, str]] = {}

    for event in events:
        task_slug = event.get("task_slug")
        if not task_slug:
            continue
        plan_name = str(event.get("plan_name") or "")
        if latest_run_only and int(event.get("id", 0)) <= latest_run_ids.get(plan_name, 0):
            continue
        state = _status_state_from_event(event)
        if state is None:
            continue
        task_statuses[(plan_name, str(task_slug))] = {
            "slug": str(task_slug),
            "state": state,
            "plan_name": plan_name,
        }

    return sorted(
        task_statuses.values(),
        key=lambda task: (task.get("plan_name", ""), task["slug"]),
    )


def _cmd_status(project_root: str, show_all: bool = False) -> None:
    """Show governor status — what's running now."""
    try:
        all_history = _status_tasks_from_events(project_root, latest_run_only=False)
        live_history = _status_tasks_from_events(project_root, latest_run_only=True)
    except Exception as exc:
        _output({"status": "error", "message": str(exc)})
        return

    if not all_history:
        _output({"status": "idle", "tasks": 0})
        return

    live = [t for t in live_history if t.get("state") in _LIVE_STATES]
    active = [t for t in live if t.get("state") == TaskState.ACTIVE.value]
    visible = all_history if show_all else live

    if want_json():
        click.echo(
            json.dumps(
                {
                    "status": "active" if live else "idle",
                    "tasks": len(all_history),
                    "active": len(active),
                    "task_list": [
                        {
                            "slug": t.get("slug"),
                            "state": t.get("state"),
                            "plan_name": t.get("plan_name"),
                        }
                        for t in visible
                    ],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"status: {'active' if live else 'idle'}")
        click.echo(f"tasks: {len(all_history)} total")
        click.echo(f"active: {len(active)}")
        if visible:
            click.echo("tasks:")
            for t in visible:
                state = t.get("state", "?")
                slug = t.get("slug", "?")
                click.echo(f"  {state:14s}  {slug}")
        elif not show_all:
            click.echo("  (no live tasks — use --all to show history)")


# Register subcommand modules — must be at bottom after cli is defined
from dgov.cli import (  # noqa: E402
    clean as clean,
    compile as compile,
    fix as fix,
    init as init,
    ledger as ledger,
    plan as plan,
    preflight as preflight,
    run as run,
    sentrux as sentrux,
    watch as watch,
)
