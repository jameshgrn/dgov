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
@click.option("--touches", multiple=True, help="Files the mission will touch (repeatable)")
@click.option("--timeout", "-t", default=600, type=int, help="Timeout per phase (seconds)")
@click.option(
    "--permission-mode", "-m", default="bypassPermissions", help="Permission mode for worker"
)
@click.option("--max-retries", default=1, type=int, help="Max retry attempts on timeout")
@click.option("--escalate-to", default=None, help="Agent to escalate to on timeout")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def mission_cmd(
    prompt,
    agent,
    auto_merge,
    slug,
    touches,
    timeout,
    permission_mode,
    max_retries,
    escalate_to,
    project_root,
    session_root,
):
    """Run a single mission: dispatch, wait, review, merge."""
    from dgov.mission import MissionPolicy, run_mission

    policy = MissionPolicy(
        agent=agent,
        auto_merge=auto_merge,
        touches=tuple(touches),
        timeout=timeout,
        permission_mode=permission_mode,
        max_retries=max_retries,
        escalate_to=escalate_to,
    )
    result = run_mission(project_root, prompt, policy, session_root, slug)
    click.echo(json.dumps(asdict(result), default=str))

    if result.state == "completed":
        click.secho(f"Mission {result.slug} completed in {result.duration_s:.1f}s", fg="green")
    elif result.state == "failed":
        click.secho(f"Mission {result.slug} failed: {result.error}", fg="red")
    elif result.state == "reviewed_pass":
        click.secho(f"Mission {result.slug} is ready to merge", fg="yellow")
    elif result.state == "review_pending":
        click.secho(
            f"Mission {result.slug} needs review ({len(result.findings or [])} findings)",
            fg="yellow",
        )
