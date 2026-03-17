"""Monitor daemon CLI command."""

import os
import shlex

import click


@click.command("monitor")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@click.option("--session-root", "-S", default=None)
@click.option("--interval", "-i", default=15, type=int, help="Poll interval seconds")
@click.option("--dry-run", is_flag=True, help="One poll cycle then exit")
@click.option("--pane", is_flag=True, help="Launch in tmux utility pane")
def monitor_cmd(project_root, session_root, interval, dry_run, pane):
    """Run 4B worker monitor daemon."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    if pane:
        from dgov.tmux import create_utility_pane

        cmd = f"dgov monitor -r {shlex.quote(project_root)} -i {interval}"
        if session_root != project_root:
            cmd += f" -S {shlex.quote(session_root)}"
        if dry_run:
            cmd += " --dry-run"
        pane_id = create_utility_pane(cmd, "[gov] monitor", cwd=project_root)
        click.echo(f'{{"pane_id": "{pane_id}"}}')
        return
    from dgov.monitor import run_monitor

    run_monitor(project_root, session_root, poll_interval=interval, dry_run=dry_run)
