"""dgov CLI — programmatic pane management for the governor."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import click

from dgov.agents import detect_installed_agents

SESSION_ROOT_OPTION: Any = click.option(
    "--session-root",
    "-S",
    default=None,
    help="Session root (where .dgov/ lives). Defaults to --project-root.",
)


def _check_governor_context() -> None:
    """Verify we're the governor: on main branch and not inside a worktree.

    Raises click.UsageError if either check fails.
    """
    if os.environ.get("DGOV_SKIP_GOVERNOR_CHECK") == "1":
        return

    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if git_dir.returncode == 0 and "worktrees" in git_dir.stdout.strip():
            raise click.UsageError(
                "dgov is running inside a git worktree. "
                "The governor must run from the main repo, not a worker worktree."
            )
    except subprocess.TimeoutExpired:
        pass

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch.returncode == 0:
            current = branch.stdout.strip()
            # Allow HEAD (detached/no commits yet) and main
            if current not in ("main", "HEAD"):
                raise click.UsageError(
                    f"Governor is on branch '{current}', but must stay on 'main'. "
                    f"Switch back with: git checkout main"
                )
    except subprocess.TimeoutExpired:
        pass


@click.group(invoke_without_command=True)
@click.option(
    "--governor", "-g", default=None, help="Override governor agent (claude, codex, gemini)"
)
@click.pass_context
def cli(ctx, governor):
    """dgov: governor + worker pane orchestration."""
    # Skip the guard for info-only commands and the bare invocation
    if ctx.invoked_subcommand not in (
        None,
        "version",
        "agents",
        "blame",
        "checkpoint",
        "experiment",
        "template",
        "openrouter",
        "dashboard",
        "stats",
        "init",
        "doctor",
    ):
        _check_governor_context()

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` — launch or announce the governor session
    from dgov.agents import (
        build_launch_command,
        get_governor_agent,
        load_registry,
        write_project_config,
    )
    from dgov.tmux import style_dgov_session, style_governor_pane

    repo = Path.cwd().name
    session_name = f"dgov-{repo}"
    project_root = str(Path.cwd())

    def _resolve_governor() -> tuple[str, str]:
        """Return (agent_id, permission_mode), running first-time setup if needed."""
        agent_id, perm = get_governor_agent(project_root)
        if governor is not None:
            agent_id = governor
        if agent_id is not None:
            return agent_id, perm or ""
        # First-time setup
        registry = load_registry(project_root)
        installed = detect_installed_agents(registry)
        if not installed:
            click.echo("No agents found on PATH. Install claude, codex, or gemini first.")
            raise SystemExit(1)
        agent_id = click.prompt(
            "Governor agent for this repo",
            type=click.Choice(installed),
            default=installed[0],
        )
        perm = click.prompt(
            "Permission mode",
            type=click.Choice(["", "plan", "acceptEdits", "bypassPermissions"]),
            default="",
        )
        assert agent_id is not None
        assert perm is not None
        write_project_config(project_root, "governor_agent", agent_id)
        if perm:
            write_project_config(project_root, "governor_permissions", perm)
        return agent_id, perm

    governor_prompt = (
        "You are the dgov governor for this repo. "
        "Use dgov CLI commands to orchestrate work:\n"
        '  dgov pane create -a <agent> -p "<task>" -r .   # dispatch a worker\n'
        "  dgov pane wait <slug>                            # wait for completion\n"
        "  dgov pane review <slug>                          # inspect the diff\n"
        "  dgov pane merge <slug>                           # merge to main\n"
        "  dgov pane close <slug>                           # cleanup\n"
        "Never edit source files directly. Dispatch workers instead."
    )

    if os.environ.get("TMUX"):
        from dgov.art import print_banner

        style_dgov_session()
        # Style the current pane as governor
        pane_id = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane_id:
            style_governor_pane(pane_id)
        print_banner()
        click.echo(f"{repo} — governor ready")

        agent_id, perm = _resolve_governor()
        registry = load_registry(project_root)
        cmd = build_launch_command(
            agent_id, prompt=governor_prompt, permission_mode=perm, registry=registry
        )
        parts = shlex.split(cmd)
        os.execvp(parts[0], parts)
    else:
        # Ensure the per-repo tmux session exists, then hand off
        exists = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if exists.returncode != 0:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name],
                capture_output=True,
                check=True,
            )
        style_dgov_session(session_name)

        agent_id, perm = _resolve_governor()
        registry = load_registry(project_root)
        launch_cmd = build_launch_command(
            agent_id, prompt=governor_prompt, permission_mode=perm, registry=registry
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, launch_cmd, "Enter"],
            capture_output=True,
        )
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


# Register subcommands
from dgov.cli.admin import (  # noqa: E402
    blame,
    dashboard,
    doctor_cmd,
    init_cmd,
    list_agents,
    preflight_cmd,
    rebase,
    stats,
    status,
    version_cmd,
)
from dgov.cli.batch_cmd import batch, checkpoint  # noqa: E402
from dgov.cli.experiment import experiment  # noqa: E402
from dgov.cli.openrouter_cmd import openrouter  # noqa: E402
from dgov.cli.pane import pane  # noqa: E402
from dgov.cli.review_fix_cmd import review_fix  # noqa: E402
from dgov.cli.templates import template  # noqa: E402

cli.add_command(pane)
cli.add_command(preflight_cmd)
cli.add_command(status)
cli.add_command(rebase)
cli.add_command(blame)
cli.add_command(list_agents)
cli.add_command(version_cmd)
cli.add_command(stats)
cli.add_command(dashboard)
cli.add_command(template)
cli.add_command(checkpoint)
cli.add_command(batch)
cli.add_command(experiment)
cli.add_command(review_fix)
cli.add_command(openrouter)
cli.add_command(init_cmd)
cli.add_command(doctor_cmd)


if __name__ == "__main__":
    cli()
