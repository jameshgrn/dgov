"""Run subcommand — plan execution with sentrux quality gates."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import click

from dgov.cli import _output, cli, want_json
from dgov.plan import compile_plan, parse_plan_file
from dgov.runner import EventDagRunner


@cli.command(name="run")
@click.argument("plan_file", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--restart", is_flag=True, help="Restart the plan from the beginning, clearing prior state"
)
@click.option(
    "--continue", "continue_failed", is_flag=True, help="Continue from where you left off, retrying failed tasks"
)
@click.option("--only", default=None, help="Run only this task and its deps")
@click.pass_context
def run_cmd(ctx: click.Context, plan_file: Path, restart: bool, continue_failed: bool, only: str | None) -> None:
    """Run a plan file (TOML).

    Example: dgov run plan.toml
    """
    if plan_file.suffix != ".toml":
        click.echo(f"Error: Plan file must be .toml, got: {plan_file}", err=True)
        raise SystemExit(1)
    project_root = str(Path.cwd())
    _cmd_run_plan(str(plan_file), project_root, restart=restart, continue_failed=continue_failed, only=only)


def _parse_quality(line: str) -> int | None:
    """Extract quality value from a 'Quality: N' or 'Quality: A -> B' line."""
    if not line.startswith("Quality:"):
        return None
    rest = line.split(":", 1)[1].strip()
    token = rest.split("->")[-1].strip() if "->" in rest else rest
    try:
        return int(token)
    except ValueError:
        return None


def _sentrux_available() -> bool:
    """Check if sentrux binary is available."""
    try:
        subprocess.run(
            ["sentrux", "--version"],
            capture_output=True,
            timeout=5.0,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _run_sentrux(args: list[str], cwd: str | None = None, timeout: float = 30.0) -> str:
    """Run sentrux command, return stdout."""
    result = subprocess.run(
        ["sentrux"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def _sentrux_save_baseline(project_root: str) -> int | None:
    """Run `sentrux gate --save` and return parsed baseline quality."""
    if not want_json():
        click.echo("[sentrux] Saving baseline...")
    try:
        result = subprocess.run(
            ["sentrux", "gate", "--save", project_root],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if not want_json():
            click.echo(f"[sentrux] Baseline save failed: {e}", err=True)
        return None
    for line in result.stdout.splitlines():
        quality = _parse_quality(line)
        if quality is not None:
            return quality
    return None


def _sentrux_compare(project_root: str, baseline_quality: int | None) -> dict[str, object]:
    """Run `sentrux gate` and build a gate_result dict comparing against baseline."""
    gate_result: dict[str, object] = {
        "degradation": None,
        "quality_before": baseline_quality,
        "quality_after": None,
    }
    if not want_json():
        click.echo("[sentrux] Comparing against baseline...")
    try:
        result = subprocess.run(
            ["sentrux", "gate", project_root],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        gate_result["error"] = str(e)
        if not want_json():
            click.echo(f"[sentrux] Gate comparison failed: {e}", err=True)
        return gate_result

    degradation = False
    quality_after: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("Quality:") and "->" in line:
            quality_after = _parse_quality(line)
        elif "No degradation" in line or "✓ No degradation" in line:
            degradation = False
        elif "degradation" in line.lower():
            degradation = True
    gate_result["degradation"] = degradation
    gate_result["quality_after"] = quality_after
    if not want_json():
        status = "✓ clean" if not degradation else "✗ degradation detected"
        click.echo(f"[sentrux] Gate result: {status}")
    return gate_result


def _make_worker_event_callback() -> Callable[[str, str, object], None]:
    """Build a callback that prints worker activity to stderr (suppressed in JSON mode)."""

    def _on_event(task_slug: str, log_type: str, content: object) -> None:
        if want_json():
            return
        if log_type == "error":
            click.echo(f"  [{task_slug}] ERROR: {content}", err=True)
        elif log_type == "thought":
            click.echo(f"  [{task_slug}] {str(content)[:120]}", err=True)
        elif log_type == "call" and isinstance(content, dict):
            tool = content.get("tool", "?")
            args = content.get("args", {})
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            click.echo(f"  [{task_slug}] {tool}({summary})", err=True)
        elif log_type == "done":
            click.echo(f"  [{task_slug}] done: {content}", err=True)

    return _on_event


def _cmd_run_plan(
    plan_file: str, project_root: str, restart: bool = False, continue_failed: bool = False, only: str | None = None
) -> None:
    """Execute a plan TOML with Sentrux quality gates."""
    from dgov.config import load_project_config

    plan = parse_plan_file(plan_file)
    pc = load_project_config(project_root)
    dag = compile_plan(plan, project_agent=pc.default_agent)

    # Filter to only specified task and its transitive dependencies
    if only is not None:
        if only not in dag.tasks:
            click.echo(f"Error: Task '{only}' not found in plan", err=True)
            raise click.exceptions.Exit(code=1)

        # BFS to collect all transitive dependencies
        to_keep: set[str] = set()
        queue = [only]
        while queue:
            slug = queue.pop()
            if slug in to_keep:
                continue
            if slug not in dag.tasks:
                continue
            to_keep.add(slug)
            task = dag.tasks[slug]
            queue.extend(task.depends_on)

        dag = dag.model_copy(
            update={"tasks": {k: v for k, v in dag.tasks.items() if k in to_keep}}
        )

    sentrux_ok = _sentrux_available()
    baseline_quality = _sentrux_save_baseline(project_root) if sentrux_ok else None

    runner = EventDagRunner(
        dag,
        session_root=project_root,
        on_event=_make_worker_event_callback(),
        restart=restart,
        continue_failed=continue_failed,
    )

    if want_json():
        click.echo(
            json.dumps(
                {"status": "starting", "dag": dag.name, "sentrux_baseline": baseline_quality}
            )
        )
    elif sentrux_ok:
        click.echo(f"[sentrux] Baseline quality: {baseline_quality}")

    try:
        start_time = datetime.now(timezone.utc)
        results = asyncio.run(runner.run())
        end_time = datetime.now(timezone.utc)
        duration = end_time - start_time
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        raise click.exceptions.Exit(code=130) from None

    gate_result = (
        _sentrux_compare(project_root, baseline_quality)
        if sentrux_ok
        else {"degradation": None, "quality_before": None, "quality_after": None}
    )

    failed = [s for s, st in results.items() if st == "failed"]
    succeeded = [s for s, st in results.items() if st == "merged"]

    _output(
        {
            "status": "complete" if not failed else "failed",
            "succeeded": len(succeeded),
            "failed": len(failed),
            "failed_tasks": failed if failed else None,
            "sentrux": gate_result if sentrux_ok else None,
            "duration_s": round(duration.total_seconds(), 2),
        }
    )

    _append_run_log(project_root, dag.name, plan_file, results, gate_result, duration)


def _append_run_log(
    project_root: str,
    plan_name: str,
    plan_file: str,
    results: dict[str, str],
    gate_result: dict[str, object],
    duration: datetime.timedelta,
) -> None:
    """Append a run summary to .dgov/runs.log — permanent, git-tracked."""
    log_path = Path(project_root) / ".dgov" / "runs.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    merged = [s for s, st in results.items() if st == "merged"]
    failed = [s for s, st in results.items() if st == "failed"]
    status = "ok" if not failed else "fail"

    lines = [f"[{ts}] {plan_name} ({plan_file}) — {status} ({round(duration.total_seconds(), 2)}s)"]
    if merged:
        lines.append(f"  merged: {', '.join(merged)}")
    if failed:
        lines.append(f"  failed: {', '.join(failed)}")
    quality_before = gate_result.get("quality_before")
    quality_after = gate_result.get("quality_after")
    if quality_before is not None:
        lines.append(f"  sentrux: {quality_before} -> {quality_after}")
    lines.append("")

    with open(log_path, "a") as f:
        f.write("\n".join(lines) + "\n")
