"""Administrative and diagnostic commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from dgov.agents import detect_installed_agents
from dgov.cli import SESSION_ROOT_OPTION


@click.command("preflight")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Agent to validate for")
@click.option("--fix", is_flag=True, help="Auto-fix fixable failures")
@click.option(
    "--touches",
    "-t",
    multiple=True,
    help="Files the task will touch (repeatable)",
)
@click.option("--branch", "-b", default=None, help="Expected branch name")
def preflight_cmd(project_root, session_root, agent, fix, touches, branch):
    """Run pre-flight checks before dispatch."""
    from dgov.agents import get_default_agent, load_registry

    if agent is None:
        agent = get_default_agent(load_registry(project_root))
    from dgov.preflight import fix_preflight, run_preflight

    report = run_preflight(
        project_root=project_root,
        agent=agent,
        touches=list(touches) if touches else None,
        expected_branch=branch,
        session_root=session_root,
    )
    if not report.passed and fix:
        report = fix_preflight(report, project_root)

    click.echo(json.dumps(report.to_dict(), indent=2))
    if not report.passed:
        sys.exit(1)


@click.command("status")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
def status(project_root, session_root):
    """Get full dgov status as JSON."""
    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    click.echo(
        json.dumps(
            {"panes": panes, "total": len(panes), "alive": sum(1 for p in panes if p["alive"])},
            indent=2,
        )
    )


@click.command("rebase")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@click.option(
    "--onto",
    default=None,
    help="Explicit base branch to rebase onto (default: auto-detect upstream or main)",
)
def rebase(project_root, onto):
    """Rebase the governor worktree onto its base branch.

    Stashes dirty changes, rebases onto upstream (or main), and pops stash.
    On conflict: aborts rebase and restores working tree.
    """
    from dgov.inspection import rebase_governor

    result = rebase_governor(project_root, onto=onto)
    click.echo(json.dumps(result, indent=2))
    if not result.get("rebased"):
        sys.exit(1)


@click.command("blame")
@click.argument("file_path")
@click.option("--project-root", "-r", default=".", help="Project root")
@click.option("--session-root", "-R", default=None, help="Session root")
@click.option("--all", "-a", "show_all", is_flag=True, default=False, help="Show full history")
@click.option("--agent", default=None, help="Filter by agent")
@click.option("--line-level", is_flag=True, default=False, help="Show line-level blame")
@click.option("--lines", "-L", default=None, help="Line range for line-level blame (e.g. 10-20)")
def blame(file_path, project_root, session_root, show_all, agent, line_level, lines):
    """Show which agent/pane last touched a file."""
    if lines or line_level:
        from dgov.blame import blame_lines

        start_line = None
        end_line = None
        if lines:
            parts = lines.split("-", 1)
            try:
                start_line = int(parts[0])
                if len(parts) > 1:
                    end_line = int(parts[1])
                else:
                    end_line = start_line
            except ValueError:
                click.echo(f"Invalid line range: {lines} (expected N or N-M)", err=True)
                sys.exit(1)

        result = blame_lines(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            start_line=start_line,
            end_line=end_line,
            agent_filter=agent,
        )
    else:
        from dgov.blame import blame_file

        result = blame_file(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            last_only=not show_all,
            agent_filter=agent,
        )
    click.echo(json.dumps(result, indent=2))


@click.command("agents")
@click.option("--project-root", "-r", default=".", help="Project root for registry loading")
def list_agents(project_root):
    """List available agents and which are installed."""
    from dgov.agents import load_registry

    registry = load_registry(project_root)
    installed = set(detect_installed_agents(registry))
    agents = []
    for agent_id, defn in registry.items():
        entry = {
            "id": agent_id,
            "name": defn.name,
            "installed": agent_id in installed,
            "transport": defn.prompt_transport,
            "source": defn.source,
        }
        if defn.health_check:
            hc = subprocess.run(defn.health_check, shell=True, capture_output=True, text=True)
            entry["healthy"] = hc.returncode == 0
        agents.append(entry)
    click.echo(json.dumps(agents, indent=2))


@click.command("version")
def version_cmd():
    """Show dgov version."""
    from dgov import __version__

    result = {"dgov": __version__}
    click.echo(json.dumps(result, indent=2))


@click.command("stats")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def stats(project_root, session_root):
    """Show pane and agent statistics."""
    from dgov.metrics import compute_stats

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    data = compute_stats(session_root)
    click.echo(json.dumps(data, indent=2))


@click.command("dashboard")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--refresh", default=2, type=float, help="Refresh interval in seconds")
def dashboard(project_root, session_root, refresh):
    """Launch live terminal dashboard."""
    from dgov.dashboard import run_dashboard

    run_dashboard(
        project_root=project_root,
        session_root=session_root,
        refresh_interval=refresh,
    )


@click.command("init")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root (where .dgov/ will be created).",
)
def init_cmd(project_root):
    """Initialize a new dgov project: scaffold .dgov/ and write config."""
    root = Path(project_root).resolve()
    config_path = root / ".dgov" / "config.toml"

    if config_path.is_file():
        click.echo("Already initialized.")
        return

    # Interactive prompts
    governor = click.prompt("Governor agent", default="claude", type=str)
    permissions = click.prompt("Permission mode", default="acceptEdits", type=str)

    # Create directories
    dirs = [
        root / ".dgov" / "hooks",
        root / ".dgov" / "templates",
        root / ".dgov" / "batch",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Write config
    config_path.write_text(
        f'[dgov]\ngovernor_agent = "{governor}"\ngovernor_permissions = "{permissions}"\n',
        encoding="utf-8",
    )

    # Add .dgov/ to .gitignore if not already there
    gitignore = root / ".gitignore"
    marker = ".dgov/"
    if gitignore.is_file():
        content = gitignore.read_text(encoding="utf-8")
        if marker not in content.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write(f"{marker}\n")
    else:
        gitignore.write_text(f"{marker}\n", encoding="utf-8")

    click.echo("Initialized dgov project:")
    click.echo(f"  {config_path}")
    for d in dirs:
        click.echo(f"  {d}/")


@click.command("doctor")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root to diagnose.",
)
def doctor_cmd(project_root):
    """Run diagnostics on the dgov environment."""
    import platform
    import shutil

    from dgov.agents import detect_installed_agents, load_registry
    from dgov.persistence import all_panes, state_path

    root = Path(project_root).resolve()
    ok = True

    def _check(label, passed, detail=""):
        nonlocal ok
        icon = "[ok]" if passed else "[FAIL]"
        if not passed:
            ok = False
        msg = f"  {icon} {label}"
        if detail:
            msg += f" -- {detail}"
        click.echo(msg)

    click.echo("dgov doctor\n")

    # 1. tmux installed
    tmux_path = shutil.which("tmux")
    _check("tmux installed", tmux_path is not None)

    # tmux server running
    if tmux_path:
        tmux_running = (
            subprocess.run(
                ["tmux", "list-sessions"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
        _check("tmux server running", tmux_running)
    else:
        _check("tmux server running", False, "tmux not installed")

    # 2. git installed
    git_path = shutil.which("git")
    _check("git installed", git_path is not None)

    # 3. Python >= 3.12
    py_ver = platform.python_version_tuple()
    py_ok = (int(py_ver[0]), int(py_ver[1])) >= (3, 12)
    _check(
        "Python >= 3.12",
        py_ok,
        f"found {platform.python_version()}",
    )

    # 4. state.db readable
    db = state_path(str(root))
    if db.is_file():
        try:
            all_panes(str(root))
            _check("state.db readable", True)
        except Exception as exc:
            _check("state.db readable", False, str(exc))
    else:
        _check("state.db exists", True, "no state.db yet (first run)")

    # 5. Installed agents
    registry = load_registry(str(root))
    installed = detect_installed_agents(registry)
    _check(
        "agents installed",
        len(installed) > 0,
        ", ".join(installed) if installed else "none found",
    )

    # 6. Hooks directory
    hooks_dir = root / ".dgov" / "hooks"
    if hooks_dir.is_dir():
        scripts = list(hooks_dir.iterdir())
        non_exec = [s.name for s in scripts if s.is_file() and not os.access(s, os.X_OK)]
        if non_exec:
            _check("hooks executable", False, f"not executable: {', '.join(non_exec)}")
        else:
            _check("hooks directory", True, f"{len(scripts)} script(s)")
    else:
        _check("hooks directory", True, "no .dgov/hooks/ (optional)")

    # 7. Orphaned worktrees
    try:
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_worktrees = []
        first = True
        for line in wt_result.stdout.splitlines():
            if line.startswith("worktree "):
                if first:
                    first = False
                    continue
                git_worktrees.append(line.split(" ", 1)[1])

        if db.is_file():
            panes = all_panes(str(root))
            tracked = {p.get("worktree_path") for p in panes}
            orphaned = [wt for wt in git_worktrees if wt not in tracked]
            _check(
                "no orphaned worktrees",
                len(orphaned) == 0,
                f"{len(orphaned)} orphaned" if orphaned else f"{len(git_worktrees)} tracked",
            )
        else:
            _check("no orphaned worktrees", True, "no state.db to compare")
    except (subprocess.TimeoutExpired, OSError) as exc:
        _check("worktree check", False, str(exc))

    # 8. Stale panes (pane in state.db whose tmux pane is dead)
    if db.is_file():
        from dgov.backend import get_backend

        backend = get_backend()
        panes = all_panes(str(root))
        active_panes = [p for p in panes if p.get("state") == "active"]
        stale = [
            p["slug"]
            for p in active_panes
            if p.get("pane_id") and not backend.is_alive(p["pane_id"])
        ]
        _check(
            "no stale panes",
            len(stale) == 0,
            f"stale: {', '.join(stale)}" if stale else f"{len(active_panes)} active",
        )

    click.echo()
    if ok:
        click.echo("All checks passed.")
    else:
        click.echo("Some checks failed.")
    sys.exit(0 if ok else 1)
