"""Fix subcommand — minimal one-off plan authoring shortcut."""

from __future__ import annotations

import re
from pathlib import Path

import click

from dgov.cli import cli
from dgov.cli.compile import _cmd_compile
from dgov.cli.run import _cmd_run_plan


def _slugify(text: str) -> str:
    """Convert text to a safe kebab-case slug."""
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    # Collapse multiple hyphens and strip leading/trailing
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Limit length for sanity
    return slug[:50] or "fix"


def _generate_plan_name(prompt: str) -> str:
    """Generate a unique plan name from the prompt, prefixed with 'fix-'."""
    slug = _slugify(prompt)
    return f"fix-{slug}"


def _render_fix_plan_toml(name: str, prompt: str, files: list[str], commit_message: str) -> str:
    """Render a single-task fix plan TOML."""
    files_str = ", ".join(f'"{f}"' for f in files)
    prompt_escaped = prompt.replace('"""', '"""')
    return (
        f"[plan]\n"
        f'name = "{name}"\n'
        f'summary = "Apply requested fix"\n'
        f'sections = ["fix"]\n'
        f"\n"
        f"[fix.apply]\n"
        f'summary = "Apply requested fix"\n'
        f'prompt = """\n'
        f"{prompt_escaped}\n"
        f'"""\n'
        f'commit_message = "{commit_message}"\n'
        f"files.edit = [{files_str}]\n"
    )


@cli.command(name="fix")
@click.argument("prompt")
@click.option(
    "--file",
    "-f",
    multiple=True,
    required=True,
    help="File to include in the fix (repeatable)",
)
@click.option("--name", help="Override the generated plan name")
@click.option(
    "--commit-message",
    default="Apply requested fix",
    help="Override the commit message",
)
@click.pass_context
def fix_cmd(
    ctx: click.Context,
    prompt: str,
    file: tuple[str, ...],
    name: str | None,
    commit_message: str,
) -> None:
    """Create and run a single-task fix plan.

    A thin wrapper around the plan pipeline that creates a one-task plan,
    compiles it, and runs it through the normal executor.

    \b
    Example: dgov fix "Refactor error handling" --file src/utils.py --file src/main.py
    """
    project_root = Path.cwd()
    plan_name = name or _generate_plan_name(prompt)
    plan_dir = project_root / ".dgov" / "plans" / plan_name

    # Fail fast if plan already exists
    if plan_dir.exists():
        click.echo(f"Error: Plan '{plan_name}' already exists at {plan_dir}", err=True)
        click.echo("Use --name to specify a different name.", err=True)
        raise click.exceptions.Exit(code=1)

    # Create plan directory structure
    plan_dir.mkdir(parents=True)
    fix_section_dir = plan_dir / "fix"
    fix_section_dir.mkdir()

    # Write _root.toml
    root_toml = f'''[plan]
name = "{plan_name}"
summary = "Apply requested fix"
sections = ["fix"]
'''
    (plan_dir / "_root.toml").write_text(root_toml)

    # Write fix/main.toml
    files_list = list(file)
    main_toml = _render_fix_plan_toml(plan_name, prompt, files_list, commit_message)
    (fix_section_dir / "main.toml").write_text(main_toml)

    click.echo(f"Created plan '{plan_name}' at {plan_dir}")

    # Compile the plan
    try:
        _cmd_compile(plan_dir, dry_run=False, recompile_sops=False, graph=False)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"Compile failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc

    # Run the compiled plan
    compiled_path = plan_dir / "_compiled.toml"
    try:
        _cmd_run_plan(
            str(compiled_path),
            str(project_root),
            restart=False,
            continue_failed=False,
            only=None,
            plan_dir=plan_dir,
        )
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"Run failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc
