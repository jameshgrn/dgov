"""Planning and prompt generation helpers."""

from __future__ import annotations

import click


@click.group()
def plan():
    """Planning and prompt generation helpers."""
    pass


@plan.command("refactor")
@click.option("--src", required=True, help="Source function/class/file (e.g. src/a.py:func)")
@click.option("--dest", required=True, help="Destination file/module (e.g. src/b.py)")
@click.option("--task", default="Move", help="Action (Move, Extract, Inline, etc.)")
def plan_refactor(src, dest, task):
    """Generate a structured prompt for a refactoring task."""
    src_file = src.split(":")[0]
    dest_file = dest.split(":")[0]

    # Extract just the name if it has a colon
    name = src.split(":")[-1] if ":" in src else src_file

    prompt = (
        f"1. Read {src_file} and {dest_file}.\n"
        f"2. {task} {name} to {dest_file}.\n"
        "3. Update ALL imports and call sites in the codebase.\n"
        "4. Run related tests to verify the change.\n"
        f"5. git add {src_file} {dest_file}\n"
        f"6. git commit -m '{task} {name} to {dest_file}'"
    )

    click.echo(prompt)
