"""Plan create subcommand — auto-generate plans via the planner agent."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from dgov.cli.plan import plan_cmd
from dgov.project_root import resolve_project_root


def _toml_str(value: str) -> str:
    """Wrap a string in TOML double quotes, escaping as needed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _toml_ml_str(value: str) -> str:
    """Render a TOML multi-line string that tolerates embedded triple quotes."""
    safe = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{safe}\n"""'


def _slugify(text: str) -> str:
    """Convert text to a safe kebab-case slug."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:50] or "plan"


def _materialize_plan(plan_data: dict, output_dir: Path) -> Path:
    """Convert emit_plan JSON to plan tree directory structure."""
    name = plan_data.get("name", "auto-plan")
    summary = plan_data.get("summary", "")
    tasks = plan_data.get("tasks", [])

    plan_dir = output_dir / name
    if plan_dir.exists():
        ts = datetime.now(UTC).strftime("%H%M%S")
        plan_dir = output_dir / f"{name}-{ts}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir = plan_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)

    # _root.toml
    root_toml = (
        f'[plan]\nname = {_toml_str(name)}\nsummary = {_toml_str(summary)}\nsections = ["tasks"]\n'
    )
    (plan_dir / "_root.toml").write_text(root_toml)

    # tasks/main.toml
    lines: list[str] = []
    for task in tasks:
        slug = task["slug"]
        lines.append(f"[tasks.{slug}]")
        lines.append(f"summary = {_toml_str(task.get('summary', slug))}")
        lines.append(f"prompt = {_toml_ml_str(task.get('prompt', ''))}")
        lines.append(f"commit_message = {_toml_str(task.get('commit_message', 'apply changes'))}")

        files = task.get("files", {})
        if isinstance(files, dict):
            for kind in ("create", "edit", "touch", "read"):
                file_list = files.get(kind, [])
                if file_list:
                    items = ", ".join(_toml_str(f) for f in file_list)
                    if kind == "touch":
                        lines.append(f"files = [{items}]")
                    else:
                        lines.append(f"files.{kind} = [{items}]")

        deps = task.get("depends_on", [])
        if deps:
            items = ", ".join(_toml_str(d) for d in deps)
            lines.append(f"depends_on = [{items}]")

        role = task.get("role", "worker")
        if role != "worker":
            lines.append(f'role = "{role}"')

        lines.append("")

    (tasks_dir / "main.toml").write_text("\n".join(lines))
    return plan_dir


def _apply_config_overrides(project_root: Path, overrides: dict) -> None:
    """Patch .dgov/project.toml with config overrides from the planner."""
    toml_path = project_root / ".dgov" / "project.toml"
    if not toml_path.exists():
        click.echo("[planner] No project.toml to patch — skipping config overrides.", err=True)
        return

    content = toml_path.read_text()
    patched = False
    allowed_keys = {
        "src_dir",
        "test_dir",
        "lint_cmd",
        "format_cmd",
        "lint_fix_cmd",
        "test_cmd",
        "language",
    }
    for key, value in overrides.items():
        if key not in allowed_keys or not isinstance(value, str) or not value.strip():
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        old_pattern = f'{key} = "'
        if old_pattern in content:
            # Replace existing value
            import re

            content = re.sub(
                rf'^({re.escape(key)}\s*=\s*").*?"',
                rf'\g<1>{escaped}"',
                content,
                count=1,
                flags=re.MULTILINE,
            )
            patched = True
            click.echo(f"[planner] Config override: {key} = {value!r}", err=True)

    if patched:
        toml_path.write_text(content)


async def _run_planner_subprocess(
    project_root: str,
    goal: str,
    model: str,
    interactive: bool,
    config_json: str,
) -> dict | None:
    """Spawn planner subprocess and handle stdin/stdout protocol."""
    from dgov.workers.headless import _PLANNER_SCRIPT

    cmd = [
        sys.executable,
        "-u",
        str(_PLANNER_SCRIPT),
        "--goal",
        goal,
        "--worktree",
        project_root,
        "--model",
        model,
        "--project-config",
        config_json,
    ]
    if interactive:
        cmd.append("--interactive")

    plan_data: dict | None = None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.STDOUT,
        cwd=project_root,
    )

    assert proc.stdout is not None

    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode().strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        ev = data.get("worker_event")
        if not ev:
            continue

        ev_type = ev.get("type")
        content = ev.get("content")

        if ev_type == "question" and interactive and proc.stdin is not None:
            click.echo(f"\n[planner] {content}", err=True)
            answer = click.prompt("  Your answer")
            payload = json.dumps({"answer": answer}) + "\n"
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
        elif ev_type == "plan":
            plan_data = content
        elif ev_type == "thought":
            # Show abbreviated thoughts
            text = str(content)[:120] if content else ""
            if text:
                click.echo(f"  [planner] {text}", err=True)
        elif ev_type == "error":
            click.echo(f"  [planner] ERROR: {content}", err=True)

    await proc.wait()
    return plan_data


@plan_cmd.command(name="create")
@click.argument("goal")
@click.option("--auto", "autonomous", is_flag=True, help="Autonomous mode (no user questions)")
@click.option(
    "--run",
    "run_plan",
    is_flag=True,
    help="Compile and run the plan after creation",
)
@click.option("--name", default=None, help="Override the generated plan name")
@click.option("--model", default=None, help="Override the planner model")
@click.option(
    "--apply-config",
    is_flag=True,
    help="Apply discovered config overrides to project.toml",
)
def plan_create_cmd(
    goal: str,
    autonomous: bool,
    run_plan: bool,
    name: str | None,
    model: str | None,
    apply_config: bool,
) -> None:
    """Auto-generate an implementation plan via the planner agent.

    Spawns a planner that explores the codebase and produces a structured
    plan. The plan is materialized to disk and optionally compiled and run.

    \b
    Examples:
      dgov plan create "Fix the auth token refresh bug"
      dgov plan create --auto "Add input validation to forms"
      dgov plan create --auto --run "Refactor error handling in api.py"
    """
    from dgov.cli.compile import _cmd_compile
    from dgov.cli.run import _cmd_run_plan
    from dgov.config import load_project_config

    project_root = resolve_project_root()
    pc = load_project_config(str(project_root))
    agent = model or pc.default_agent
    config_json = json.dumps(pc.to_worker_payload())
    interactive = not autonomous

    click.echo(f"[planner] Mode: {'interactive' if interactive else 'autonomous'}", err=True)
    click.echo(f"[planner] Model: {agent}", err=True)
    click.echo(f"[planner] Goal: {goal[:100]}{'...' if len(goal) > 100 else ''}", err=True)

    # Run planner subprocess
    plan_data = asyncio.run(
        _run_planner_subprocess(str(project_root), goal, agent, interactive, config_json)
    )

    if not plan_data:
        click.echo("Error: Planner did not emit a plan.", err=True)
        raise click.exceptions.Exit(code=1)

    # Apply config overrides if requested
    overrides = plan_data.get("config_overrides", {})
    if overrides and apply_config:
        _apply_config_overrides(project_root, overrides)
    elif overrides:
        click.echo("[planner] Suggested config overrides (use --apply-config to apply):", err=True)
        for k, v in overrides.items():
            click.echo(f"  {k} = {v!r}", err=True)

    # Materialize plan tree
    plans_dir = project_root / ".dgov" / "runtime" / "auto-plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    if name:
        plan_data["name"] = name
    elif not plan_data.get("name"):
        plan_data["name"] = _slugify(goal)

    plan_dir = _materialize_plan(plan_data, plans_dir)
    task_count = len(plan_data.get("tasks", []))
    click.echo(f"[planner] Plan emitted: {plan_data['name']} ({task_count} task(s))", err=True)
    click.echo(f"  Created at {plan_dir}", err=True)

    # Compile
    try:
        _cmd_compile(plan_dir, dry_run=False, recompile_sops=False, graph=False)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"Compile failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc

    if not run_plan:
        click.echo(f"\n  To run: dgov run {plan_dir}", err=True)
        return

    # Run
    compiled_path = plan_dir / "_compiled.toml"
    try:
        _cmd_run_plan(
            str(compiled_path),
            str(project_root),
            restart=False,
            continue_failed=False,
            only=None,
            plan_dir=plan_dir,
            yes=True,
        )
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"Run failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc
