"""Verify recipe CLI — run project-local verification recipes."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.config import load_project_config
from dgov.project_root import resolve_project_root
from dgov.verify import run_verify_recipe


@cli.group(name="verify")
def verify_cmd() -> None:
    """Run project-local verification recipes defined in .dgov/project.toml."""
    pass


@verify_cmd.command(name="list")
@click.option("--root", "-r", default=".", help="Project root")
def verify_list(root: str) -> None:
    """List configured verification recipes."""
    project_root = resolve_project_root(Path(root))
    config = load_project_config(project_root)
    recipes = config.verify_recipes

    if want_json():
        payload = {
            "recipes": [
                {
                    "name": name,
                    "description": recipe.description,
                    "command": recipe.command,
                }
                for name, recipe in recipes.items()
            ]
        }
        click.echo(json.dumps(payload, indent=2))
        return

    if not recipes:
        click.echo("No verification recipes configured.")
        return

    click.echo("Verification recipes:")
    for name, recipe in recipes.items():
        desc = f" — {recipe.description}" if recipe.description else ""
        click.echo(f"  {name}{desc}")


@verify_cmd.command(name="run")
@click.argument("name")
@click.option("--root", "-r", default=".", help="Project root")
def verify_run(name: str, root: str) -> None:
    """Run a single verification recipe by name."""
    project_root = resolve_project_root(Path(root))
    config = load_project_config(project_root)
    recipes = config.verify_recipes

    if name not in recipes:
        click.echo(f"Error: unknown verify recipe '{name}'", err=True)
        raise click.exceptions.Exit(code=1)

    recipe = recipes[name]
    result = run_verify_recipe(project_root, recipe)

    if want_json():
        payload = {
            "status": result.status,
            "recipe": name,
            "command": recipe.command,
            "results": [
                {
                    "exit_code": r.exit_code,
                    "duration_s": r.duration_s,
                    "warning_count": r.warning_count,
                    "log_path": r.log_path,
                    "summary": r.summary,
                }
                for r in result.results
            ],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        for r in result.results:
            status_label = "PASS" if r.exit_code == 0 else "FAIL"
            click.echo(f"{status_label}: {name}")
            click.echo(f"  exit_code: {r.exit_code}")
            click.echo(f"  duration: {r.duration_s:.2f}s")
            click.echo(f"  warnings: {r.warning_count}")
            click.echo(f"  log: {r.log_path}")

    if result.status != "pass":
        raise click.exceptions.Exit(code=1)
