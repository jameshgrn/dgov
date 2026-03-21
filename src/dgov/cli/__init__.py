"""dgov CLI — programmatic pane management for the governor."""

from __future__ import annotations

import os
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

_GOVERNOR_PERMISSION_MODE = "bypassPermissions"
_GOVERNOR_PROMPT = (
    "You are the dgov governor for this repo. "
    "Use dgov CLI commands to orchestrate work:\n"
    '  dgov pane create -a <agent> -p "<task>" -r .   # dispatch a worker\n'
    "  dgov pane wait <slug>                            # wait for completion\n"
    "  dgov pane review <slug>                          # inspect the diff\n"
    "  dgov pane land <slug>                           # review, merge, close\n"
    "  dgov pane close <slug>                           # cleanup\n"
    "Never edit source files directly. Dispatch workers instead."
)


def _check_governor_context() -> None:
    """Verify we're the governor: on main branch and not inside a worktree.

    Raises click.UsageError if either check fails.
    """
    if os.environ.get("DGOV_SKIP_GOVERNOR_CHECK") == "1":
        return

    if "DGOV_SLUG" in os.environ:
        raise click.UsageError(
            "dgov governor commands cannot be run from within a worker pane. "
            "Workers must use `dgov worker complete` or standard git/build tools."
        )

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
            if current not in ("main", "HEAD"):
                raise click.UsageError(
                    f"Governor is on branch '{current}', but must stay on 'main'. "
                    f"Switch back with: git checkout main"
                )
    except subprocess.TimeoutExpired:
        pass


def _normalize_governor_permissions(project_root: str, permission_mode: str | None) -> str:
    """Force governor launches onto the max-permission mode and persist it."""
    from dgov.agents import write_project_config

    if permission_mode != _GOVERNOR_PERMISSION_MODE:
        write_project_config(project_root, "governor_permissions", _GOVERNOR_PERMISSION_MODE)
    return _GOVERNOR_PERMISSION_MODE


def _tmux_has_session(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _ensure_tmux_session(session_name: str, project_root: str) -> bool:
    """Ensure the repo-scoped tmux session exists. Returns True if created."""
    if _tmux_has_session(session_name):
        return False
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", project_root],
        capture_output=True,
        check=True,
    )
    return True


def _list_tmux_panes(target: str) -> list[tuple[str, str, str]]:
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_id}|#{pane_title}|#{pane_current_command}",
        ],
        capture_output=True,
        text=True,
    )
    panes: list[tuple[str, str, str]] = []
    if result.returncode != 0:
        return panes
    for line in result.stdout.splitlines():
        pane_id, sep, rest = line.partition("|")
        if not sep:
            continue
        title, sep, command = rest.partition("|")
        if not sep:
            continue
        panes.append((pane_id, title, command))
    return panes


def _ensure_governor_session(
    project_root: str,
    session_name: str,
    agent_id: str,
    permission_mode: str,
) -> tuple[bool, bool]:
    """Ensure session/workspace exist and governor is running.

    Returns (session_created, governor_started).
    """
    from dgov.agents import build_launch_command, load_registry
    from dgov.tmux import setup_governor_workspace, style_dgov_session, style_governor_pane

    session_created = _ensure_tmux_session(session_name, project_root)
    style_dgov_session(session_name)

    target_window = f"{session_name}:0"
    setup_governor_workspace(project_root, target_window=target_window)

    registry = load_registry(project_root)
    launch_cmd = build_launch_command(
        agent_id,
        prompt=_GOVERNOR_PROMPT,
        permission_mode=permission_mode,
        registry=registry,
    )
    agent_def = registry.get(agent_id)
    prompt_command = agent_def.prompt_command if agent_def else agent_id

    panes = _list_tmux_panes(target_window)
    governor_pane_id = ""
    for pane_id, title, command in panes:
        if title == "[gov] main" or command == prompt_command:
            governor_pane_id = pane_id
            break

    if governor_pane_id:
        style_governor_pane(governor_pane_id)
        return session_created, False

    if not panes:
        return session_created, False

    governor_pane_id = panes[0][0]
    style_governor_pane(governor_pane_id)
    subprocess.run(
        ["tmux", "send-keys", "-t", governor_pane_id, launch_cmd, "Enter"],
        capture_output=True,
    )
    return session_created, True


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
        "resume",
        "version",
        "agents",
        "blame",
        "checkpoint",
        "template",
        "openrouter",
        "dashboard",
        "briefing",
        "stats",
        "init",
        "doctor",
        "yap",
        "terrain",
        "worker",
        "monitor",
        "codebase",
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
    from dgov.tmux import setup_governor_workspace, style_dgov_session, style_governor_pane

    repo = Path.cwd().name
    session_name = f"dgov-{repo}"
    project_root = str(Path.cwd())

    # Auto-init if not yet initialized
    from dgov.cli.admin import _scaffold_dgov_dirs
    from dgov.lifecycle import ensure_dgov_gitignored

    config_path = Path(project_root) / ".dgov" / "config.toml"
    if not config_path.is_file():
        _scaffold_dgov_dirs(Path(project_root))
    ensure_dgov_gitignored(project_root)

    def _resolve_governor() -> tuple[str, str]:
        """Return (agent_id, permission_mode), prompting on first use."""
        agent_id, perm = get_governor_agent(project_root)
        if governor is not None:
            agent_id = governor
            write_project_config(project_root, "governor_agent", agent_id)
        if agent_id is not None:
            return agent_id, _normalize_governor_permissions(project_root, perm)
        # First-time setup — prompt for preferences
        registry = load_registry(project_root)
        installed = detect_installed_agents(registry)
        if not installed:
            click.echo("No agents found on PATH. Install claude, codex, or gemini first.")
            raise SystemExit(1)
        default_agent = "claude" if "claude" in installed else installed[0]
        agent_id = click.prompt(
            "Governor agent for this repo",
            type=click.Choice(installed),
            default=default_agent,
        )
        assert agent_id is not None
        write_project_config(project_root, "governor_agent", agent_id)
        perm = _normalize_governor_permissions(project_root, None)
        return agent_id, perm

    if os.environ.get("TMUX"):
        style_dgov_session()
        # Style the current pane as governor
        pane_id = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane_id:
            style_governor_pane(pane_id)
        if os.environ.get("TERM") in ("dumb", "emacs"):
            click.echo("dgov — dispatch · wait · review · merge")
        else:
            click.echo(
                "\n"
                " ██████   ██████  ██████ ██   ██\n"
                " ██   ██ ██      ██   ██ ██   ██\n"
                " ██   ██ ██  ███ ██   ██ ██   ██\n"
                " ██   ██ ██   ██ ██   ██  ██ ██\n"
                " ██████   ██████  ██████   ████\n"
                "\033[2m  dispatch · wait · review · merge\033[0m\n"
            )
        click.echo(f"{repo} — governor ready")
        setup_governor_workspace(project_root)

        agent_id, perm = _resolve_governor()
        registry = load_registry(project_root)
        cmd = build_launch_command(
            agent_id, prompt=_GOVERNOR_PROMPT, permission_mode=perm, registry=registry
        )
        os.execvp("sh", ["sh", "-c", cmd])
    else:
        agent_id, perm = _resolve_governor()
        _ensure_governor_session(project_root, session_name, agent_id, perm)
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


@cli.command("resume")
@click.pass_context
def resume_cmd(ctx):
    """Resume an existing dgov governor session."""
    from dgov.agents import get_governor_agent

    repo = Path.cwd().name
    session_name = f"dgov-{repo}"
    project_root = str(Path.cwd())

    agent_id, perm = get_governor_agent(project_root)
    if not agent_id:
        click.echo(f"No saved governor config found for '{repo}'. Run `dgov` to configure.")
        raise SystemExit(1)
    normalized_perm = _normalize_governor_permissions(project_root, perm)

    created, started = _ensure_governor_session(
        project_root,
        session_name,
        agent_id,
        normalized_perm,
    )
    if created:
        click.echo(f"{repo} — rebuilt governor session from saved config")
    elif started:
        click.echo(f"{repo} — governor relaunched in existing session")

    if os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", session_name])
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


@cli.command("refresh")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
def refresh_cmd(project_root):
    """Reinstall dgov from source and restart workspace panes."""
    import signal

    import dgov as _dgov_mod

    project_root = os.path.abspath(project_root)

    # 0. Resolve session name FIRST — we need it to scope all tmux operations
    repo = Path(project_root).name
    session_name = f"dgov-{repo}"
    from dgov.agents import get_governor_agent

    agent_id, perm = get_governor_agent(project_root)
    if not agent_id:
        click.secho(
            f"No saved governor config for '{repo}'. Run `dgov` to configure first.",
            fg="yellow",
        )
        raise SystemExit(1)
    normalized_perm = _normalize_governor_permissions(project_root, perm)

    # 1. Reinstall
    click.secho("Reinstalling dgov...", fg="yellow")
    _dgov_src = Path(_dgov_mod.__file__).resolve().parent.parent.parent
    if (_dgov_src / "pyproject.toml").is_file():
        result = subprocess.run(
            ["uv", "tool", "install", "--force", "--python", "3.14", "-e", str(_dgov_src)],
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            ["uv", "tool", "upgrade", "dgov"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        click.secho(f"Install failed: {result.stderr}", fg="red")
        raise SystemExit(1)
    click.secho("Installed.", fg="green")

    # 2. Kill stale dashboard + monitor daemons so workspace setup restarts them
    for name in ("dashboard", "monitor"):
        pidfile = Path(project_root) / ".dgov" / f"{name}.pid"
        if not pidfile.is_file():
            continue
        try:
            old_pid = int(pidfile.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            click.echo(f"Killed stale {name} (pid {old_pid})")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        pidfile.unlink(missing_ok=True)

    # 3. Kill utility panes — scoped to THIS session only
    if _tmux_has_session(session_name):
        try:
            panes = subprocess.run(
                [
                    "tmux",
                    "list-panes",
                    "-s",
                    "-t",
                    session_name,
                    "-F",
                    "#{pane_id} #{pane_title}",
                ],
                capture_output=True,
                text=True,
            )
            if panes.returncode == 0:
                for line in panes.stdout.splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] in (
                        "[gov] dashboard",
                        "[gov] terrain",
                    ):
                        subprocess.run(
                            ["tmux", "kill-pane", "-t", parts[0]],
                            capture_output=True,
                        )
                        click.echo(f"Killed {parts[1]}")
        except OSError:
            pass

    # 4. Rebuild or refresh the session/workspace
    created, started = _ensure_governor_session(
        project_root,
        session_name,
        agent_id,
        normalized_perm,
    )
    if os.environ.get("TMUX"):
        if created:
            click.secho("Workspace refreshed (session rebuilt).", fg="green")
        else:
            click.secho("Workspace refreshed.", fg="green")
    else:
        if created:
            click.secho("Workspace refreshed (session rebuilt). Attaching...", fg="green")
        elif started:
            click.secho("Workspace refreshed (governor relaunched). Attaching...", fg="green")
        else:
            click.secho("Workspace refreshed. Attaching...", fg="green")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


# Register subcommands
from dgov.cli.admin import (  # noqa: E402
    blame,
    codebase_cmd,
    dashboard,
    doctor_cmd,
    gc_cmd,
    init_cmd,
    list_agents,
    preflight_cmd,
    rebase,
    stats,
    status,
    terrain_cmd,
    tunnel_cmd,
    version_cmd,
)
from dgov.cli.batch_cmd import batch, checkpoint  # noqa: E402
from dgov.cli.briefing_cmd import briefing_cmd  # noqa: E402
from dgov.cli.dag_cmd import dag  # noqa: E402
from dgov.cli.journal_cmd import journal_cmd  # noqa: E402
from dgov.cli.merge_queue_cmd import merge_queue  # noqa: E402
from dgov.cli.mission_cmd import mission_cmd  # noqa: E402
from dgov.cli.monitor_cmd import monitor_cmd  # noqa: E402
from dgov.cli.openrouter_cmd import openrouter  # noqa: E402
from dgov.cli.pane import pane  # noqa: E402
from dgov.cli.review_fix_cmd import review_fix  # noqa: E402
from dgov.cli.templates import template  # noqa: E402
from dgov.cli.worker_cmd import worker  # noqa: E402

cli.add_command(pane)
cli.add_command(preflight_cmd)
cli.add_command(status)
cli.add_command(rebase)
cli.add_command(blame)
cli.add_command(codebase_cmd)
cli.add_command(list_agents)
cli.add_command(version_cmd)
cli.add_command(stats)
cli.add_command(dashboard)
cli.add_command(template)
cli.add_command(checkpoint)
cli.add_command(batch)
cli.add_command(review_fix)
cli.add_command(openrouter)
cli.add_command(init_cmd)
cli.add_command(doctor_cmd)
cli.add_command(gc_cmd)
cli.add_command(mission_cmd)
cli.add_command(dag)
cli.add_command(merge_queue)
cli.add_command(briefing_cmd)
cli.add_command(terrain_cmd)
cli.add_command(tunnel_cmd)
cli.add_command(worker)
cli.add_command(monitor_cmd)
cli.add_command(journal_cmd)


if __name__ == "__main__":
    cli()
