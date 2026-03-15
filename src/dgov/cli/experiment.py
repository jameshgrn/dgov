"""Experiment loop commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group()
def experiment():
    """Manage experiment loops."""


@experiment.command("start")
@click.option(
    "--program", "-p", required=True, type=click.Path(exists=True), help="Program file (markdown)"
)
@click.option("--metric", "-m", required=True, help="Metric name to optimize")
@click.option("--budget", "-b", default=5, help="Max experiments to run")
@click.option("--agent", "-a", default=None, help="Agent to use")
@click.option("--direction", "-d", type=click.Choice(["minimize", "maximize"]), default="minimize")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Timeout per experiment in seconds")
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
def experiment_start(
    program, metric, budget, agent, direction, project_root, session_root, timeout, dry_run
):
    """Run an experiment loop."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.experiment import run_experiment_loop

    if agent is None:
        agent = get_default_agent(load_registry(project_root))

    session_root_abs = os.path.abspath(session_root or project_root)

    if dry_run:
        program_name = Path(program).stem
        click.echo(
            json.dumps(
                {
                    "dry_run": True,
                    "program": program,
                    "program_name": program_name,
                    "metric": metric,
                    "budget": budget,
                    "agent": agent,
                    "direction": direction,
                },
                indent=2,
            )
        )
        return

    for result in run_experiment_loop(
        project_root=project_root,
        program_path=program,
        metric_name=metric,
        budget=budget,
        agent=agent,
        direction=direction,
        session_root=session_root_abs,
        timeout=timeout,
    ):
        if isinstance(result, dict):
            click.echo(json.dumps(result))


@experiment.command("log")
@click.option("--program", "-p", required=True, help="Program name (stem of the program file)")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def experiment_log(program, project_root, session_root):
    """Show the experiment log as JSON."""
    from dgov.experiment import ExperimentLog

    session_root_abs = os.path.abspath(session_root or project_root)
    log = ExperimentLog(session_root_abs, program)
    if not log.path.exists():
        click.echo(
            json.dumps({"warning": f"No experiment log found for program: {program}"}), err=True
        )
    entries = log.read_log()
    click.echo(json.dumps(entries, indent=2))


@experiment.command("summary")
@click.option("--program", "-p", required=True, help="Program name (stem of the program file)")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--direction", "-d", type=click.Choice(["minimize", "maximize"]), default="minimize")
def experiment_summary(program, project_root, session_root, direction):
    """Show summary stats for an experiment program."""
    from dgov.experiment import ExperimentLog

    session_root_abs = os.path.abspath(session_root or project_root)
    log = ExperimentLog(session_root_abs, program)
    if not log.path.exists():
        click.echo(
            json.dumps({"warning": f"No experiment log found for program: {program}"}), err=True
        )
    click.echo(json.dumps(log.summary(direction), indent=2))
