"""Plan create subcommand — auto-generate plans via the planner agent."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

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
    plan_dir = _unique_plan_dir(output_dir, str(name))
    tasks_dir = plan_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)

    (plan_dir / "_root.toml").write_text(_root_toml(str(name), str(summary)))
    (tasks_dir / "main.toml").write_text(_tasks_toml(plan_data.get("tasks", [])))
    return plan_dir


def _unique_plan_dir(output_dir: Path, name: str) -> Path:
    plan_dir = output_dir / name
    if plan_dir.exists():
        ts = datetime.now(UTC).strftime("%H%M%S")
        plan_dir = output_dir / f"{name}-{ts}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _root_toml(name: str, summary: str) -> str:
    return (
        f'[plan]\nname = {_toml_str(name)}\nsummary = {_toml_str(summary)}\nsections = ["tasks"]\n'
    )


def _tasks_toml(tasks: object) -> str:
    lines: list[str] = []
    if not isinstance(tasks, list):
        return ""
    for task in tasks:
        if isinstance(task, dict):
            lines.extend(_task_toml_lines(task))
    return "\n".join(lines)


def _task_toml_lines(task: dict) -> list[str]:
    slug = task["slug"]
    lines = [
        f"[tasks.{slug}]",
        f"summary = {_toml_str(task.get('summary', slug))}",
        f"prompt = {_toml_ml_str(task.get('prompt', ''))}",
        f"commit_message = {_toml_str(task.get('commit_message', 'apply changes'))}",
    ]
    lines.extend(_task_files_lines(task.get("files", {})))
    lines.extend(_task_dep_lines(task.get("depends_on", [])))
    lines.extend(_task_role_lines(task.get("role", "worker")))
    lines.append("")
    return lines


def _task_files_lines(files: object) -> list[str]:
    lines: list[str] = []
    if isinstance(files, dict):
        file_map = cast("dict[str, object]", files)
        for kind in ("create", "edit", "touch", "read"):
            file_list = file_map.get(kind, [])
            if file_list:
                items = _toml_array(file_list)
                key = "files" if kind == "touch" else f"files.{kind}"
                lines.append(f"{key} = [{items}]")
    return lines


def _toml_array(values: object) -> str:
    if not isinstance(values, list):
        return ""
    return ", ".join(_toml_str(str(value)) for value in values)


def _task_dep_lines(deps: object) -> list[str]:
    if not deps:
        return []
    return [f"depends_on = [{_toml_array(deps)}]"]


def _task_role_lines(role: object) -> list[str]:
    if role == "worker":
        return []
    return [f"role = {_toml_str(str(role))}"]


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


def _planner_command(
    project_root: str,
    goal: str,
    model: str,
    interactive: bool,
    config_json: str,
) -> list[str]:
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
    return cmd


async def _read_planner_event(proc: asyncio.subprocess.Process) -> dict | None:
    assert proc.stdout is not None
    line_bytes = await proc.stdout.readline()
    if not line_bytes:
        return None
    line = line_bytes.decode().strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


async def _handle_planner_event(
    ev: dict,
    *,
    interactive: bool,
    proc: asyncio.subprocess.Process,
) -> dict | None:
    ev_type = ev.get("type")
    content = ev.get("content")

    if ev_type == "question" and interactive and proc.stdin is not None:
        click.echo(f"\n[planner] {content}", err=True)
        answer = click.prompt("  Your answer")
        payload = json.dumps({"answer": answer}) + "\n"
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()
    elif ev_type == "plan":
        return content if isinstance(content, dict) else None
    elif ev_type == "thought":
        _echo_planner_thought(content)
    elif ev_type == "error":
        click.echo(f"  [planner] ERROR: {content}", err=True)
    return None


def _echo_planner_thought(content: object) -> None:
    text = str(content)[:120] if content else ""
    if text:
        click.echo(f"  [planner] {text}", err=True)


async def _run_planner_subprocess(
    project_root: str,
    goal: str,
    model: str,
    interactive: bool,
    config_json: str,
) -> dict | None:
    """Spawn planner subprocess and handle stdin/stdout protocol."""
    plan_data: dict | None = None
    proc = await asyncio.create_subprocess_exec(
        *_planner_command(project_root, goal, model, interactive, config_json),
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.STDOUT,
        cwd=project_root,
    )

    while True:
        data = await _read_planner_event(proc)
        if data is None:
            break

        ev = data.get("worker_event")
        if not ev:
            continue

        emitted_plan = await _handle_planner_event(ev, interactive=interactive, proc=proc)
        if emitted_plan is not None:
            plan_data = emitted_plan

    await proc.wait()
    return plan_data


def _echo_planner_header(*, interactive: bool, agent: str, goal: str) -> None:
    click.echo(f"[planner] Mode: {'interactive' if interactive else 'autonomous'}", err=True)
    click.echo(f"[planner] Model: {agent}", err=True)
    click.echo(f"[planner] Goal: {goal[:100]}{'...' if len(goal) > 100 else ''}", err=True)


def _maybe_apply_or_report_config(
    project_root: Path,
    plan_data: dict,
    *,
    apply_config: bool,
) -> None:
    overrides = plan_data.get("config_overrides", {})
    if overrides and apply_config:
        _apply_config_overrides(project_root, overrides)
    elif overrides:
        click.echo("[planner] Suggested config overrides (use --apply-config to apply):", err=True)
        for key, value in overrides.items():
            click.echo(f"  {key} = {value!r}", err=True)


def _finalize_plan_name(plan_data: dict, *, name: str | None, goal: str) -> None:
    if name:
        plan_data["name"] = name
    elif not plan_data.get("name"):
        plan_data["name"] = _slugify(goal)


def _materialize_auto_plan(
    project_root: Path,
    plan_data: dict,
    *,
    name: str | None,
    goal: str,
) -> Path:
    plans_dir = project_root / ".dgov" / "runtime" / "auto-plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    _finalize_plan_name(plan_data, name=name, goal=goal)
    plan_dir = _materialize_plan(plan_data, plans_dir)
    task_count = len(plan_data.get("tasks", []))
    click.echo(f"[planner] Plan emitted: {plan_data['name']} ({task_count} task(s))", err=True)
    click.echo(f"  Created at {plan_dir}", err=True)
    return plan_dir


def _compile_auto_plan(plan_dir: Path) -> None:
    from dgov.cli.compile import compile_plan_dir

    try:
        compile_plan_dir(plan_dir, dry_run=False, recompile_sops=False, graph=False)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"Compile failed: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from exc


def _run_auto_plan(project_root: Path, plan_dir: Path) -> None:
    from dgov.cli.run import run_compiled_plan

    compiled_path = plan_dir / "_compiled.toml"
    try:
        run_compiled_plan(
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


def _plan_create_settings(
    project_root: Path,
    *,
    model: str | None,
    autonomous: bool,
) -> tuple[str, str, bool]:
    from dgov.config import load_project_config

    pc = load_project_config(str(project_root))
    agent = model or pc.default_agent
    return agent, json.dumps(pc.to_worker_payload()), not autonomous


def _run_planner_or_exit(
    project_root: Path,
    *,
    goal: str,
    agent: str,
    interactive: bool,
    config_json: str,
) -> dict:
    plan_data = asyncio.run(
        _run_planner_subprocess(str(project_root), goal, agent, interactive, config_json)
    )
    if plan_data:
        return plan_data
    click.echo("Error: Planner did not emit a plan.", err=True)
    raise click.exceptions.Exit(code=1)


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
    project_root = resolve_project_root()
    agent, config_json, interactive = _plan_create_settings(
        project_root,
        model=model,
        autonomous=autonomous,
    )
    _echo_planner_header(interactive=interactive, agent=agent, goal=goal)
    plan_data = _run_planner_or_exit(
        project_root,
        goal=goal,
        agent=agent,
        interactive=interactive,
        config_json=config_json,
    )

    _maybe_apply_or_report_config(project_root, plan_data, apply_config=apply_config)
    plan_dir = _materialize_auto_plan(project_root, plan_data, name=name, goal=goal)
    _compile_auto_plan(plan_dir)

    if not run_plan:
        click.echo(f"\n  To run: dgov run {plan_dir}", err=True)
        return

    _run_auto_plan(project_root, plan_dir)
