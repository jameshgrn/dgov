"""dgov CLI — unified HFT surface. One command, context-aware dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import click

from dgov import __version__

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


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.argument("target", required=False)
@click.option("--status", is_flag=True, help="Show governor status")
@click.option("--watch", is_flag=True, help="Start governor daemon/watch mode")
@click.option("--json", is_flag=True, help="Output as JSON")
@click.version_option(version=__version__, prog_name="dgov")
@click.pass_context
def cli(
    ctx: click.Context,
    target: str | None,
    status: bool,
    watch: bool,
    json: bool,
) -> None:
    """dgov — unified governor surface.

    \b
    USAGE:
      dgov                    Show status
      dgov plan.toml          Run a plan
      dgov --watch            Start governor daemon

    Everything routes through the governor event loop.
    """
    if json:
        os.environ["DGOV_JSON"] = "1"

    project_root = str(Path.cwd())

    # Route by intent
    if status or (target is None and not watch):
        _cmd_status(project_root)
        return

    if watch:
        _cmd_watch(project_root)
        return

    if target:
        target_path = Path(target)
        if target_path.suffix == ".toml":
            _cmd_run_plan(target, project_root)
        else:
            click.echo(f"Unknown target: {target}", err=True)
            raise click.Exit(code=1)
        return

    click.echo(cli.get_help(ctx))


def _cmd_status(project_root: str) -> None:
    """Show governor status — what's running now."""
    from dgov.persistence import all_panes

    try:
        panes = all_panes(project_root)
    except Exception as exc:
        _output({"status": "error", "message": str(exc)})
        return

    if not panes:
        _output({"status": "idle", "panes": 0})
        return

    active = [p for p in panes if p.get("state") == "active"]
    _output(
        {
            "status": "active" if active else "idle",
            "panes": len(panes),
            "active": len(active),
            "pane_list": [{"slug": p.get("slug"), "state": p.get("state")} for p in panes[:10]],
        }
    )


def _cmd_watch(project_root: str) -> None:
    """Start governor watch mode — stream events."""
    from dgov.persistence import read_events

    click.echo("Governor watch mode (Ctrl-C to exit)")
    click.echo("-" * 40)

    try:
        import time

        last_id = 0
        while True:
            events = read_events(project_root, limit=50)
            new_events = [e for e in events if e.get("id", 0) > last_id]
            for ev in new_events:
                click.echo(f"[{ev.get('ts', '?')}] {ev.get('event', '?')}: {ev.get('pane', '?')}")
                last_id = max(last_id, ev.get("id", 0))
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nExiting watch mode.")


def _cmd_run_plan(plan_file: str, project_root: str) -> None:
    """Execute a plan TOML."""
    from dgov.plan import compile_plan, parse_plan_file
    from dgov.runner import EventDagRunner

    plan = parse_plan_file(plan_file)
    dag = compile_plan(plan)

    runner = EventDagRunner(dag, session_root=project_root)

    if want_json():
        click.echo(json.dumps({"status": "starting", "dag": dag.name}))

    try:
        results = asyncio.run(runner.run())
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        raise click.Exit(code=130)

    succeeded = [s for s, st in results.items() if st == "merged"]
    failed = [s for s, st in results.items() if st == "failed"]

    _output(
        {
            "status": "complete" if not failed else "failed",
            "succeeded": len(succeeded),
            "failed": len(failed),
            "failed_tasks": failed if failed else None,
        }
    )

    if failed:
        raise click.exceptions.Exit(code=1)


if __name__ == "__main__":
    cli()
