"""Run a unit file.

Usage: dgov run <unit.toml> [--dry-run]

A unit is a single task: goal → dispatch → track → signal.
Follows design principles:
- Canonical 4-stage dispatch: compile → preflight → spawn → track
- Zero polling: event-driven via pipe
- Trace everything
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import click

from dgov.runner import EventDagRunner
from dgov.spans import SpanContext, annotate, flush, print_trace
from dgov.unit import UnitSpec
from dgov.unit_compile import compile_unit

logger = logging.getLogger(__name__)


def parse_unit_toml(path: str) -> UnitSpec:
    """Parse unit TOML into UnitSpec.

    Minimal format:
    name = "add-hello"
    goal = "Create a hello.txt file"
    """
    import tomllib

    with open(path, "rb") as f:
        data = tomllib.load(f)

    return UnitSpec(
        name=data["name"],
        goal=data["goal"],
    )


@click.command("run")
@click.argument("unit_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be dispatched without executing",
)
@click.option(
    "--session-root",
    "-r",
    default=".",
    help="Project root directory",
)
@click.pass_context
def run(
    ctx: click.Context,
    unit_file: Path,
    dry_run: bool,
    session_root: str,
) -> None:
    """Run a unit: compile → dispatch → track → signal."""
    session_root = Path(session_root).resolve()

    # Ensure spans directory exists
    spans_dir = session_root / ".dgov" / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)

    # Generate trace ID
    trace_id = f"unit-run-{int(time.time() * 1000)}"

    with SpanContext("unit_run", str(session_root), trace_id):
        annotate("unit_file", str(unit_file))
        annotate("dry_run", dry_run)

        # --- Phase 1: Parse unit ---
        with SpanContext("parse_unit", str(session_root), trace_id):
            try:
                unit = parse_unit_toml(str(unit_file))
                annotate("unit_name", unit.name)
                annotate("unit_goal", unit.goal)
            except Exception as e:
                logger.error("Unit parsing failed: %s", e)
                annotate("error", str(e))
                click.echo(f"Error: Unit parsing failed: {e}", err=True)
                sys.exit(1)

        # --- Phase 2: Compile to DAG ---
        with SpanContext("compile_unit", str(session_root), trace_id):
            dag = compile_unit(unit)
            annotate("tasks_count", len(dag.tasks))

        if dry_run:
            click.echo(f'[unit] {unit.name}: "{unit.goal}"')
            click.echo(f"Would dispatch: {list(dag.tasks.keys())[0]} (mock agent)")
            flush(str(session_root))
            return

        # --- Phase 3: Execute ---
        click.echo(f"[unit] {unit.name}: {unit.goal}")

        with SpanContext("execute_unit", str(session_root), trace_id):
            runner = EventDagRunner(
                dag=dag,
                session_root=str(session_root),
            )

            try:
                task_states = asyncio.run(runner.run())
                annotate("completed_tasks", len(task_states))

                state = list(task_states.values())[0] if task_states else "UNKNOWN"
                annotate("final_state", state)

                if state == "MERGED":
                    click.echo(f"✓ Completed: {unit.name}")
                elif state == "FAILED":
                    click.echo(f"✗ Failed: {unit.name}", err=True)
                    sys.exit(1)
                else:
                    click.echo(f"? State: {state}")

            except Exception as e:
                annotate("error", str(e))
                logger.error("Unit execution failed: %s", e)
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    # Flush spans
    flush(str(session_root))

    # Show trace
    click.echo(f"\nTrace: {trace_id}")
    print_trace(trace_id, str(session_root))
