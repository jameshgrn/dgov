"""Run subcommand — plan execution with sentrux quality gates."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import click

from dgov.archive import archive_plan
from dgov.cli import _output, cli, want_json
from dgov.deploy_log import is_plan_complete
from dgov.plan import compile_plan, parse_plan_file
from dgov.runner import EventDagRunner


@cli.command(name="run")
@click.argument("plan", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--restart", is_flag=True, help="Restart the plan from the beginning, clearing prior state"
)
@click.option(
    "--continue",
    "continue_failed",
    is_flag=True,
    help="Continue from where you left off, retrying failed tasks",
)
@click.option("--only", default=None, help="Run only this task and its deps")
@click.pass_context
def run_cmd(
    ctx: click.Context, plan: Path, restart: bool, continue_failed: bool, only: str | None
) -> None:
    """Run a compiled plan (_compiled.toml or plan directory).

    Example: dgov run .dgov/plans/my-plan/
    """
    plan_file = plan
    plan_dir: Path | None = None
    if plan.is_dir():
        plan_file = plan / "_compiled.toml"
        if not plan_file.exists():
            click.echo(f"Error: No _compiled.toml found in {plan}", err=True)
            click.echo("Run 'dgov compile <dir>' first.", err=True)
            raise SystemExit(1)
        plan_dir = plan

    if plan_file.suffix != ".toml":
        click.echo(f"Error: Plan file must be .toml, got: {plan_file}", err=True)
        raise SystemExit(1)

    project_root = str(Path.cwd())
    _cmd_run_plan(
        str(plan_file),
        project_root,
        restart=restart,
        continue_failed=continue_failed,
        only=only,
        plan_dir=plan_dir,
    )


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
        ["sentrux", *args],
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
    import json as _json

    gate_result: dict[str, object] = {
        "degradation": None,
        "quality_before": baseline_quality,
        "quality_after": None,
    }
    if not want_json():
        click.echo("[sentrux] Comparing against baseline...")

    # Skip comparison when baseline was from an empty project (no import edges).
    baseline_path = Path(project_root) / ".sentrux" / "baseline.json"
    if baseline_path.exists():
        try:
            bdata = _json.loads(baseline_path.read_text())
            if bdata.get("total_import_edges", 0) == 0:
                gate_result["degradation"] = False
                if not want_json():
                    click.echo("[sentrux] Gate result: ✓ clean (empty baseline skipped)")
                return gate_result
        except Exception:
            pass

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
            data = cast("dict[str, object]", content)
            tool = data.get("tool", "?")
            args = cast("dict[str, object]", data.get("args", {}))
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            click.echo(f"  [{task_slug}] {tool}({summary})", err=True)
        elif log_type == "done":
            click.echo(f"  [{task_slug}] done: {content}", err=True)

    return _on_event


def _cmd_run_plan(
    plan_file: str,
    project_root: str,
    restart: bool = False,
    continue_failed: bool = False,
    only: str | None = None,
    plan_dir: Path | None = None,
) -> None:
    """Execute a plan TOML with Sentrux quality gates."""
    import os

    from dgov.config import load_project_config

    plan = parse_plan_file(plan_file)

    # Pillar #4: Determinism - Only run compiled plans.
    # Bypass for bootstrap/tests via DGOV_ALLOW_UNCOMPILED=1
    if not plan.sop_set_hash and not os.environ.get("DGOV_ALLOW_UNCOMPILED"):
        click.echo(f"Error: Plan {plan_file} is not compiled.", err=True)
        click.echo("dgov requires plans to be compiled via the Plan Tree pipeline.", err=True)
        click.echo("To fix this:", err=True)
        click.echo("1. Ensure your plan is in a directory with a _root.toml.", err=True)
        click.echo("2. Run: dgov compile <dir>", err=True)
        click.echo("3. Run: dgov run <dir>/_compiled.toml", err=True)
        raise click.exceptions.Exit(code=1)

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
            json.dumps({
                "status": "starting",
                "dag": dag.name,
                "sentrux_baseline": baseline_quality,
            })
        )
    elif sentrux_ok:
        click.echo(f"[sentrux] Baseline quality: {baseline_quality}")

    try:
        start_time = datetime.now(UTC)
        results = asyncio.run(runner.run())
        end_time = datetime.now(UTC)
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
    abandoned = [s for s, st in results.items() if st in ("abandoned", "timed_out")]
    skipped = [s for s, st in results.items() if st == "skipped"]
    succeeded = [s for s, st in results.items() if st == "merged"]
    task_errors = {slug: err for slug, err in runner._task_errors.items() if slug in failed}

    any_bad = failed or abandoned
    if not any_bad:
        run_status = "complete"
    elif succeeded:
        run_status = "partial"
    else:
        run_status = "failed"

    if task_errors and not want_json():
        for slug, err in task_errors.items():
            click.echo(f"  {slug}: {err[:200]}")

    if abandoned and not want_json():
        click.echo(
            f"  {len(abandoned)} task(s) abandoned from a prior crashed run. "
            "Use `dgov run --continue` to retry them.",
            err=True,
        )

    _output({
        "status": run_status,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "abandoned": len(abandoned) if abandoned else None,
        "skipped": len(skipped) if skipped else None,
        "failed_tasks": failed if failed else None,
        "abandoned_tasks": abandoned if abandoned else None,
        "task_errors": task_errors if task_errors else None,
        "sentrux": gate_result if sentrux_ok else None,
        "duration_s": round(duration.total_seconds(), 2),
    })

    _append_run_log(
        project_root,
        dag.name,
        plan_file,
        results,
        gate_result,
        duration,
        runner._task_durations,
        task_errors,
    )

    # Auto-archive when all units are deployed. Suppressed for --only runs (intentionally partial).
    if (
        run_status == "complete"
        and only is None
        and plan_dir is not None
        and is_plan_complete(project_root, dag.name, set(dag.tasks))
    ):
        dest = archive_plan(plan_dir)
        if not want_json():
            click.echo(f"Plan fully deployed → archived to {dest}")


def _append_run_log(
    project_root: str,
    plan_name: str,
    plan_file: str,
    results: dict[str, str],
    gate_result: dict[str, object],
    duration: timedelta,
    task_durations: dict[str, float] | None = None,
    task_errors: dict[str, str] | None = None,
) -> None:
    """Append a run summary to .dgov/runs.log — permanent, git-tracked."""
    log_path = Path(project_root) / ".dgov" / "runs.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    merged = [s for s, st in results.items() if st == "merged"]
    failed = [s for s, st in results.items() if st == "failed"]
    abandoned = [s for s, st in results.items() if st in ("abandoned", "timed_out")]
    status = "ok" if not failed and not abandoned else "fail"

    lines = [
        f"[{ts}] {plan_name} ({plan_file}) — {status} ({round(duration.total_seconds(), 2)}s)"
    ]
    if merged:
        lines.append(f"  merged: {', '.join(merged)}")
    if failed:
        lines.append(f"  failed: {', '.join(failed)}")
    if abandoned:
        lines.append(f"  abandoned: {', '.join(abandoned)}")
    if task_errors:
        for slug, err in task_errors.items():
            lines.append(f"    error[{slug}]: {err[:200]}")
    if task_durations:
        dur_str = ", ".join(f"{s}: {d}s" for s, d in task_durations.items())
        lines.append(f"  durations: {dur_str}")
    quality_before = gate_result.get("quality_before")
    quality_after = gate_result.get("quality_after")
    if quality_before is not None:
        lines.append(f"  sentrux: {quality_before} -> {quality_after}")
    lines.append("")

    with log_path.open("a") as f:
        f.write("\n".join(lines) + "\n")
