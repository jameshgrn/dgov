"""CLI command for the mission primitive."""

from __future__ import annotations

import json
from dataclasses import asdict

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.command("mission")
@click.argument("prompt")
@click.option("--agent", "-a", default="claude", help="Agent to dispatch")
@click.option("--auto-merge", is_flag=True, default=False, help="Merge on review pass")
@click.option("--slug", "-s", default=None, help="Custom slug")
@click.option("--timeout", "-t", default=600, type=int, help="Timeout per phase (seconds)")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def mission_cmd(prompt, agent, auto_merge, slug, timeout, project_root, session_root):
    """Run a single mission: dispatch, wait, review, merge."""
    from dgov.mission import MissionPolicy, run_mission

    policy = MissionPolicy(agent=agent, auto_merge=auto_merge, timeout=timeout)
    result = run_mission(project_root, prompt, policy, session_root, slug)
    click.echo(json.dumps(asdict(result), default=str))

    if result.state == "completed":
        click.secho(f"Mission {result.slug} completed in {result.duration_s:.1f}s", fg="green")
    elif result.state == "failed":
        click.secho(f"Mission {result.slug} failed: {result.error}", fg="red")
    elif result.state == "review_pending":
        click.secho(
            f"Mission {result.slug} needs review ({len(result.findings or [])} findings)",
            fg="yellow",
        )
