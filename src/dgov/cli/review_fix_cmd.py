"""Review-fix pipeline command."""

from __future__ import annotations

import json
import sys

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.command("review-fix")
@click.option(
    "--targets", "-t", required=True, multiple=True, help="File/directory paths to review"
)
@click.option("--review-agent", default=None, help="Agent for review phase")
@click.option("--fix-agent", default=None, help="Agent for fix phase")
@click.option(
    "--auto-approve", is_flag=True, default=False, help="Proceed to fix phase automatically"
)
@click.option(
    "--severity",
    type=click.Choice(["critical", "medium", "low"]),
    default="medium",
    help="Severity threshold (critical=only critical, medium=critical+medium, low=all)",
)
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--timeout", default=600, help="Timeout per phase in seconds")
def review_fix(
    targets, review_agent, fix_agent, auto_approve, severity, project_root, session_root, timeout
):
    """Run review-then-fix pipeline: review targets, collect findings, optionally fix."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.review_fix import run_review_fix_pipeline

    if review_agent is None or fix_agent is None:
        default = get_default_agent(load_registry(project_root))
        review_agent = review_agent or default
        fix_agent = fix_agent or default

    result = run_review_fix_pipeline(
        project_root=project_root,
        targets=list(targets),
        review_agent=review_agent,
        fix_agent=fix_agent,
        session_root=session_root,
        auto_approve=auto_approve,
        severity_threshold=severity,
        timeout=timeout,
    )
    click.echo(json.dumps(result, indent=2))
    if result.get("failed_count", 0) > 0:
        sys.exit(1)
