"""dgov CLI — headless governor surface."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import cast

import click

from dgov import __version__
from dgov.live_state import LIVE_STATES as _LIVE_STATES, tasks_from_events
from dgov.persistence import prune_runtime_artifact_history
from dgov.project_root import resolve_project_root

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


def load_project_config_or_exit(root: str | Path):
    """Load project config for CLI commands, converting parse failures to Click errors."""
    from dgov.config import load_project_config

    try:
        return load_project_config(root)
    except ValueError as exc:
        raise click.ClickException(f"Project configuration error: {exc}") from None


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
      dgov agents sync           Install/update shipped dgov agent skills
      dgov init-plan <name>      Initialize a new plan directory
      dgov fix <prompt>          Create and run a one-off fix plan
      dgov kb validate           Validate the repo knowledge base
      dgov watch                 Stream events live
      dgov tools audit           Summarize worker tool-call telemetry
      dgov archive-plan <name>   Manually archive a plan
      dgov plan create <goal>    Auto-generate an implementation plan
      dgov plan status <dir>     Show pending vs deployed units
      dgov plan remediate <dir>  Scaffold a follow-up remediation plan
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
    """Prune historical runtime artifact rows — removes abandoned and closed records."""
    project_root = str(resolve_project_root())
    try:
        count = prune_runtime_artifact_history(project_root)
        if count == 0:
            click.echo("Nothing to prune.")
        else:
            click.echo(f"Pruned {count} historical task(s).")
    except Exception as exc:
        click.echo(f"Prune failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc


def _cmd_status(project_root: str, show_all: bool = False) -> None:
    """Show governor status — what's running now."""
    try:
        all_history = tasks_from_events(project_root, latest_run_only=False)
        live_history = tasks_from_events(project_root, latest_run_only=True)
    except Exception as exc:
        _output({"status": "error", "message": str(exc)})
        return

    if not all_history:
        _output({"status": "idle", "tasks": 0})
        return

    status = _status_view(all_history, live_history, show_all)
    if want_json():
        click.echo(json.dumps(_status_payload(status), indent=2))
    else:
        _echo_status_text(status, show_all)


def _status_view(
    all_history: list[dict],
    live_history: list[dict],
    show_all: bool,
) -> dict[str, object]:
    live = [t for t in live_history if t.get("state") in _LIVE_STATES]
    active = [t for t in live if t.get("state") == "active"]
    settling = [t for t in live if t.get("state") == "settling"]
    attention_states = {"reviewed_fail", "reviewed_pass"}
    attention = [t for t in live if t.get("state") in attention_states]
    visible = all_history if show_all else live
    state_counts: dict[str, int] = {}
    for t in live:
        state = t.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    has_non_attention = any(t.get("state") not in attention_states for t in live)
    if live and has_non_attention:
        top_status = "active"
    elif attention:
        top_status = "needs_attention"
    else:
        top_status = "idle"
    return {
        "status": top_status,
        "tasks": len(all_history),
        "active": len(active),
        "settling": len(settling),
        "attention": len(attention),
        "state_counts": state_counts,
        "visible": visible,
    }


def _status_payload(status: dict[str, object]) -> dict[str, object]:
    visible = cast("list[dict]", status["visible"])
    return {
        "status": status["status"],
        "tasks": status["tasks"],
        "active": status["active"],
        "settling": status["settling"],
        "attention": status["attention"],
        "state_counts": status["state_counts"],
        "task_list": [_status_task_payload(task) for task in visible],
    }


def _status_task_payload(task: dict) -> dict[str, object]:
    return {
        "slug": task.get("slug"),
        "state": task.get("state"),
        "plan_name": task.get("plan_name"),
        "phase": task.get("phase"),
    }


def _echo_status_text(status: dict[str, object], show_all: bool) -> None:
    visible = cast("list[dict]", status["visible"])
    click.echo(f"status: {status['status']}")
    click.echo(f"tasks: {status['tasks']} total")
    click.echo(f"active: {status['active']}")
    if status["settling"]:
        click.echo(f"settling: {status['settling']}")
    if status["attention"]:
        click.echo(f"attention: {status['attention']}")
    if visible:
        click.echo("tasks:")
        for task in visible:
            _echo_status_task(task)
    elif not show_all:
        click.echo("  (no live tasks — use --all to show history)")


def _echo_status_task(task: dict) -> None:
    state = task.get("state", "?")
    slug = task.get("slug", "?")
    phase = task.get("phase")
    if phase:
        click.echo(f"  {state:14s}  {slug}  ({phase})")
    else:
        click.echo(f"  {state:14s}  {slug}")


# Register subcommand modules — must be at bottom after cli is defined
from dgov.cli import (  # noqa: E402
    agents as agents,
    clean as clean,
    compile as compile,
    coverage as coverage,
    diagnose as diagnose,
    fix as fix,
    init as init,
    kb as kb,
    ledger as ledger,
    plan as plan,
    preflight as preflight,
    run as run,
    scope as scope,
    sentrux as sentrux,
    tools as tools,
    verify as verify,
    watch as watch,
)
