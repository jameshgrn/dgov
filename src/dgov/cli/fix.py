"""Fix subcommand — minimal one-off plan authoring shortcut."""

from __future__ import annotations

import re
from pathlib import Path

import click

from dgov.archive import archive_plan
from dgov.cli import cli
from dgov.cli.compile import compile_plan_dir
from dgov.cli.run import run_compiled_plan
from dgov.project_root import resolve_project_root


def _fix_plans_dir(project_root: Path) -> Path:
    """Return the runtime directory for generated fix plans."""
    return project_root / ".dgov" / "runtime" / "fix-plans"


def _slugify(text: str) -> str:
    """Convert text to a safe kebab-case slug."""
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    # Collapse multiple hyphens and strip leading/trailing
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Limit length for sanity
    return slug[:50] or "fix"


def _generate_plan_name(prompt: str) -> str:
    """Generate a base plan name from the prompt, prefixed with 'fix-'."""
    slug = _slugify(prompt)
    return f"fix-{slug}"


def _toml_str(value: str) -> str:
    """Wrap a string in TOML double quotes, escaping as needed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _toml_ml_str(value: str) -> str:
    """Render a TOML multi-line string that tolerates embedded triple quotes."""
    safe = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{safe}\n"""'


_PROMPT_PHASE_RE = re.compile(r"^\s*(?:#{1,6}\s+)?(?:\*\*)?(orient|edit|verify)", re.I | re.M)


def _has_structured_prompt(prompt: str) -> bool:
    phases = {match.group(1).lower() for match in _PROMPT_PHASE_RE.finditer(prompt)}
    return {"orient", "edit", "verify"} <= phases


def _render_structured_fix_prompt(prompt: str, files: tuple[str, ...]) -> str:
    """Wrap a one-line fix request in the standard Orient/Edit/Verify shape."""
    if _has_structured_prompt(prompt):
        return prompt
    file_list = "\n".join(f"- `{path}`" for path in files)
    verify_lines = _fix_verify_lines(files)
    return (
        "Orient:\n"
        f"- Requested fix: {prompt}\n"
        "- Read the claimed files before editing:\n"
        f"{file_list}\n"
        "- Stay inside the declared file claims.\n\n"
        "Edit:\n"
        "1. Apply only the requested fix.\n"
        "2. Keep unrelated cleanup out of the diff.\n\n"
        "Verify:\n"
        f"{verify_lines}\n"
        "- Review `git diff` before calling done."
    )


def _fix_verify_lines(files: tuple[str, ...]) -> str:
    python_files = [path for path in files if path.endswith(".py")]
    if not python_files:
        return "- Run the narrowest project-specific check for the changed file(s)."
    targets = " ".join(python_files)
    return f"- `uv run ruff check {targets}`\n- `uv run ruff format --check {targets}`"


def _fix_task_summary(prompt: str) -> str:
    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    return first_line[:100] or "Apply requested fix"


def _render_fix_plan_toml(
    prompt: str,
    files: list[str],
    commit_message: str,
    summary: str,
) -> str:
    """Render the single task file for a fix plan tree."""
    files_str = ", ".join(_toml_str(f) for f in files)
    return (
        "[tasks.apply]\n"
        f"summary = {_toml_str(summary)}\n"
        f"prompt = {_toml_ml_str(prompt)}\n"
        f"commit_message = {_toml_str(commit_message)}\n"
        f"files = [{files_str}]\n"
    )


def _archive_if_exists(plan_dir: Path) -> None:
    """Archive plan_dir if it still exists. Silent noop if already moved or deleted."""
    if plan_dir.exists():
        archive_plan(plan_dir)


def _allocate_fix_plan_dir(
    plans_dir: Path,
    archive_dir: Path,
    *,
    prompt: str,
    explicit_name: str | None,
) -> tuple[str, Path]:
    """Allocate a runtime fix plan dir, failing on unresolved live collisions."""
    if explicit_name:
        plan_name = explicit_name
        plan_dir = plans_dir / plan_name
        archive_path = archive_dir / plan_name
        if plan_dir.exists() or archive_path.exists():
            existing_path = plan_dir if plan_dir.exists() else archive_path
            click.echo(f"Error: Plan '{plan_name}' already exists at {existing_path}", err=True)
            click.echo("Use --name to specify a different name.", err=True)
            raise click.exceptions.Exit(code=1)
        return plan_name, plan_dir

    base_name = _generate_plan_name(prompt)
    live_plan_dir = plans_dir / base_name
    if live_plan_dir.exists():
        click.echo(f"Error: unresolved fix plan already exists at {live_plan_dir}", err=True)
        click.echo(
            "Fix: retry or inspect that plan, archive it, or use --name for a distinct fix.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    plan_name = base_name
    plan_dir = live_plan_dir
    suffix = 2
    while plan_dir.exists() or (archive_dir / plan_name).exists():
        plan_name = f"{base_name}-{suffix}"
        plan_dir = plans_dir / plan_name
        suffix += 1
    return plan_name, plan_dir


def _create_fix_plan(
    plan_dir: Path,
    plan_name: str,
    prompt: str,
    files: tuple[str, ...],
    commit_message: str,
) -> None:
    plan_dir.mkdir(parents=True)
    fix_section_dir = plan_dir / "fix"
    fix_section_dir.mkdir()
    structured_prompt = _render_structured_fix_prompt(prompt, files)

    root_toml = f'''[plan]
name = "{plan_name}"
summary = "Apply requested fix"
sections = ["fix"]
'''
    (plan_dir / "_root.toml").write_text(root_toml)
    main_toml = _render_fix_plan_toml(
        structured_prompt,
        list(files),
        commit_message,
        _fix_task_summary(prompt),
    )
    (fix_section_dir / "main.toml").write_text(main_toml)


def _compile_fix_plan(plan_dir: Path) -> None:
    try:
        compile_plan_dir(plan_dir, dry_run=False, recompile_sops=False, graph=False)
    except click.exceptions.Exit:
        click.echo(f"Retained unresolved fix plan at {plan_dir}", err=True)
        raise
    except Exception as exc:
        click.echo(f"Compile failed: {exc}", err=True)
        click.echo(f"Retained unresolved fix plan at {plan_dir}", err=True)
        raise click.exceptions.Exit(code=1) from exc


def _run_fix_plan(plan_dir: Path, project_root: Path) -> str:
    compiled_path = plan_dir / "_compiled.toml"
    try:
        return run_compiled_plan(
            str(compiled_path),
            str(project_root),
            restart=False,
            continue_failed=False,
            only=None,
            plan_dir=plan_dir,
        )
    except click.exceptions.Exit:
        click.echo(f"Retained unresolved fix plan at {plan_dir}", err=True)
        raise
    except Exception as exc:
        click.echo(f"Run failed: {exc}", err=True)
        click.echo(f"Retained unresolved fix plan at {plan_dir}", err=True)
        raise click.exceptions.Exit(code=1) from exc


def _finish_fix_plan(plan_dir: Path, run_status: str) -> None:
    if run_status == "complete":
        _archive_if_exists(plan_dir)
    else:
        click.echo(f"Retained unresolved fix plan at {plan_dir}")


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
def fix_cmd(
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
    project_root = resolve_project_root()
    plans_dir = _fix_plans_dir(project_root)
    plans_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = plans_dir / "archive"
    plan_name, plan_dir = _allocate_fix_plan_dir(
        plans_dir,
        archive_dir,
        prompt=prompt,
        explicit_name=name,
    )

    _create_fix_plan(plan_dir, plan_name, prompt, file, commit_message)
    click.echo(f"Created plan '{plan_name}' at {plan_dir}")
    _compile_fix_plan(plan_dir)
    _finish_fix_plan(plan_dir, _run_fix_plan(plan_dir, project_root))
