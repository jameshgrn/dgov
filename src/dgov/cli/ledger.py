"""Ledger subcommands — operational memory CLI surface."""

from __future__ import annotations

from pathlib import Path

import click

from dgov.cli import cli
from dgov.persistence import add_ledger_entry, list_ledger_entries, resolve_ledger_entry
from dgov.project_root import resolve_project_root

#: Valid ledger entry categories
LEDGER_CATEGORIES = ("bug", "fix", "rule", "pattern", "note", "debt", "capability", "decision")


@cli.group(name="ledger")
def ledger_cmd() -> None:
    """Operational ledger — bugs, fixes, rules, patterns, notes, debt, capabilities, decisions."""
    pass


@ledger_cmd.command(name="add")
@click.argument("category", type=click.Choice(LEDGER_CATEGORIES))
@click.argument("content")
@click.option(
    "--path",
    "affected_paths",
    multiple=True,
    help="Path claim the entry applies to. Repeat for multiple paths.",
)
@click.option("--root", "-r", default=".", help="Project root")
def ledger_add(category: str, content: str, affected_paths: tuple[str, ...], root: str) -> None:
    """Add an entry to the ledger."""
    project_root = str(resolve_project_root(Path(root)))
    entry_id = add_ledger_entry(
        project_root,
        category,
        content,
        affected_paths=affected_paths,
    )
    click.echo(f"Added {category} entry #{entry_id}")


@ledger_cmd.command(name="list")
@click.option(
    "--category",
    "-c",
    type=click.Choice(LEDGER_CATEGORIES),
    help="Filter by category",
)
@click.option(
    "--status",
    "-s",
    type=click.Choice(["open", "resolved"]),
    default="open",
    help="Filter by status",
)
@click.option("--query", "-q", help="Search content by keyword")
@click.option("--root", "-r", default=".", help="Project root")
def ledger_list(category: str | None, status: str, query: str | None, root: str) -> None:
    """List ledger entries."""
    project_root = str(resolve_project_root(Path(root)))
    entries = list_ledger_entries(project_root, category=category, status=status, query=query)

    if not entries:
        if category:
            click.echo(f"No {status} {category}s found.")
        else:
            click.echo(f"No {status} entries found.")
        return

    for entry in entries:
        # category colors for visual distinction
        _cat_colors = {
            "bug": "yellow",
            "fix": "green",
            "rule": "cyan",
            "pattern": "bright_blue",
            "note": "blue",
            "debt": "magenta",
            "capability": "bright_green",
            "decision": "bright_magenta",
        }
        cat_colored = click.style(
            f"[{entry.category}]", fg=_cat_colors.get(entry.category, "yellow")
        )
        id_str = click.style(f"#{entry.id}", fg="black", dim=True)
        click.echo(f"{id_str} {cat_colored} {entry.content}")


@ledger_cmd.command(name="resolve")
@click.argument("entry_id", type=int)
@click.option("--root", "-r", default=".", help="Project root")
def ledger_resolve(entry_id: int, root: str) -> None:
    """Mark a ledger entry as resolved."""
    project_root = str(resolve_project_root(Path(root)))
    if resolve_ledger_entry(project_root, entry_id):
        click.echo(f"Resolved entry #{entry_id}")
    else:
        click.echo(f"Entry #{entry_id} not found.")
