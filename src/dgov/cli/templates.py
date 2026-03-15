"""Prompt template commands."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group()
def template():
    """Manage prompt templates."""


@template.command("list")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def template_list(project_root, session_root):
    """List all available templates (built-in + user)."""
    from dgov.templates import list_templates

    session_root_abs = os.path.abspath(session_root or project_root)
    click.echo(json.dumps(list_templates(session_root_abs), indent=2))


@template.command("show")
@click.argument("name")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def template_show(name, project_root, session_root):
    """Show template details and required variables."""
    from dgov.templates import load_templates

    session_root_abs = os.path.abspath(session_root or project_root)
    templates = load_templates(session_root_abs)
    if name not in templates:
        click.echo(f"Unknown template: {name}. Available: {', '.join(templates)}", err=True)
        sys.exit(1)
    tpl = templates[name]
    click.echo(
        json.dumps(
            {
                "name": tpl.name,
                "description": tpl.description,
                "template": tpl.template,
                "required_vars": tpl.required_vars,
                "default_agent": tpl.default_agent,
            },
            indent=2,
        )
    )


@template.command("create")
@click.argument("name")
def template_create(name):
    """Create a new template file in .dgov/templates/."""
    session_root = os.path.abspath(".")
    templates_dir = Path(session_root) / ".dgov" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    out_path = templates_dir / f"{name}.toml"
    if out_path.exists():
        click.echo(f"Template already exists: {out_path}", err=True)
        sys.exit(1)
    content = (
        f'name = "{name}"\n'
        'description = ""\n'
        'template = "Do {{thing}} in {{file}}. Commit."\n'
        'required_vars = ["thing", "file"]\n'
        'default_agent = "pi"\n'
    )
    out_path.write_text(content)
    click.echo(json.dumps({"created": str(out_path)}))
