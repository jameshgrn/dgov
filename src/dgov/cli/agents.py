"""Machine agent skill installation commands."""

from __future__ import annotations

from pathlib import Path

import click

from dgov.agent_skills import sync_agent_skills
from dgov.cli import cli


@cli.group(name="agents")
def agents_cmd() -> None:
    """Install dgov-maintained machine agent guidance."""
    pass


@agents_cmd.command(name="sync")
@click.option(
    "--skills-dir",
    type=click.Path(path_type=Path),
    default="~/.agents/skills",
    show_default=True,
    help="Local agent skill directory to update.",
)
def agents_sync_cmd(skills_dir: Path) -> None:
    """Sync shipped dgov skills into the local agent skill directory."""
    result = sync_agent_skills(skills_dir)
    target_root = skills_dir.expanduser()
    click.echo(f"Synced dgov agent skills to {target_root}")
    _print_paths("created", result.created)
    _print_paths("updated", result.updated)
    if not result.changed:
        click.echo("All shipped dgov skills were already current.")


def _print_paths(label: str, paths: tuple[Path, ...]) -> None:
    if not paths:
        return
    click.echo(f"{label}:")
    for path in paths:
        click.echo(f"  {path}")
