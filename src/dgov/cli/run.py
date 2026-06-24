"""Run subcommand — plan execution with sentrux quality gates."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import click

from dgov.cli import (
    _output,
    cli,
    load_project_config_or_exit,
    run_checks,
    run_git,
    run_lifecycle,
    run_output,
    run_record,
    want_json,
)
from dgov.config import ProjectConfig
from dgov.dag_parser import DagDefinition
from dgov.plan import PlanSpec, compile_plan, parse_plan_file
from dgov.project_root import ProjectPathError, resolve_project_path, resolve_project_root
from dgov.run_source import current_run_source
from dgov.runner import EventDagRunner

_clean_head_worktree = run_checks._clean_head_worktree
_normalize_sentrux_assessment = run_checks._normalize_sentrux_assessment


def sentrux_available() -> bool:
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


def run_sentrux(
    args: list[str], cwd: str | None = None, timeout: float = 30.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run sentrux command."""
    result = subprocess.run(
        ["sentrux", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
    return result


@cli.command(name="run")
@click.argument("plan", type=click.Path(path_type=Path))
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
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts (auto-create bootstrap commits)"
)
@click.option(
    "--stream",
    is_flag=True,
    help="Stream worker thoughts and tool calls live (like `dgov watch` inline)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show richer per-task summary at end of run",
)
@click.pass_context
def run_cmd(
    ctx: click.Context,
    plan: Path,
    restart: bool,
    continue_failed: bool,
    only: str | None,
    yes: bool,
    stream: bool,
    verbose: bool,
) -> None:
    """Compile and run a plan directory.

    Example: dgov run .dgov/plans/my-plan/
    """
    project_root_path, plan_dir = _resolve_run_plan_dir(plan)
    project_root = str(project_root_path)
    run_git.block_dirty_committed_worktree(project_root)
    compile_plan_for_run(plan_dir)
    plan_file = plan_dir / "_compiled.toml"
    run_compiled_plan(
        str(plan_file),
        project_root,
        restart=restart,
        continue_failed=continue_failed,
        only=only,
        plan_dir=plan_dir,
        yes=yes,
        stream=stream,
        verbose=verbose,
    )


def _resolve_run_plan_dir(plan: Path) -> tuple[Path, Path]:
    project_root_path = resolve_project_root()
    try:
        plan_dir = resolve_project_path(project_root_path, plan, label="plan path")
    except ProjectPathError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None
    _require_run_plan_dir(plan_dir)
    return project_root_path, plan_dir


def _require_run_plan_dir(plan_dir: Path) -> None:
    if not plan_dir.exists():
        click.echo(f"Error: plan path not found: {plan_dir}", err=True)
        click.echo("Fix: pass a plan directory under this project root.", err=True)
        raise click.exceptions.Exit(code=1)
    if plan_dir.is_dir():
        return
    click.echo("Error: dgov run requires a plan directory, not a file path.", err=True)
    click.echo(
        "Fix: run `dgov run <plan-dir>` so dgov compiles the current source first.", err=True
    )
    raise SystemExit(1)


def compile_plan_for_run(plan_dir: Path) -> None:
    """Compile the current plan tree before every public run."""
    from dgov.cli.compile import compile_plan_dir

    project_root = resolve_project_root()
    try:
        plan_dir = resolve_project_path(project_root, plan_dir, label="plan path")
    except ProjectPathError as exc:
        raise click.ClickException(str(exc)) from None

    compile_plan_dir(plan_dir, dry_run=False, recompile_sops=False, graph=False)


def _require_sentrux_baseline(project_root: str) -> int | None:
    return run_checks.require_sentrux_baseline(
        project_root,
        run_sentrux=run_sentrux,
        sentrux_available=sentrux_available,
    )


def _sentrux_compare(
    project_root: str,
    baseline_quality: int | None,
    base_ref: str | None = None,
    config: ProjectConfig | None = None,
) -> dict[str, object]:
    return run_checks.sentrux_compare(
        project_root,
        baseline_quality,
        base_ref=base_ref,
        config=config,
        run_sentrux=run_sentrux,
        want_json=want_json,
    )


def _branch_verification_gate_from_base(
    project_root: str,
    config: object,
    base_ref: str | None,
) -> dict[str, object]:
    return run_checks.branch_verification_gate_from_base(
        project_root,
        config,
        base_ref,
        git_stdout=run_git.git_stdout,
    )


def _make_worker_event_callback(stream: bool = False) -> Callable[[str, str, object], None]:
    """Build a callback that prints worker activity to stderr.

    In the default (non-stream) mode, only `error` and `done` events are
    surfaced. Pass `stream=True` to also print the full thought / tool-call
    firehose — equivalent to the old default behavior, and what `dgov watch`
    shows in a second pane.

    JSON mode suppresses all event callback output regardless.
    """

    def _on_event(task_slug: str, log_type: str, content: object) -> None:
        if want_json():
            return
        if log_type == "error":
            click.echo(f"  [{task_slug}] ERROR: {content}", err=True)
            return
        if log_type == "done":
            click.echo(f"  [{task_slug}] done: {content}", err=True)
            return
        if not stream:
            return
        if log_type == "thought":
            click.echo(f"  [{task_slug}] {str(content)[:120]}", err=True)
        elif log_type == "call" and isinstance(content, dict):
            data = cast("dict[str, object]", content)
            tool = data.get("tool", "?")
            args = cast("dict[str, object]", data.get("args", {}))
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            click.echo(f"  [{task_slug}] {tool}({summary})", err=True)

    return _on_event


def _ensure_compiled_plan(plan: PlanSpec, plan_file: str) -> None:
    if getattr(plan, "source_mtime_max", None) or os.environ.get("DGOV_ALLOW_UNCOMPILED"):
        return
    click.echo(f"Error: Plan {plan_file} is not compiled.", err=True)
    click.echo("dgov requires plans to be compiled via the Plan Tree pipeline.", err=True)
    click.echo("To fix this:", err=True)
    click.echo("1. Ensure your plan is in a directory with a _root.toml.", err=True)
    click.echo("2. Run: dgov compile <dir>", err=True)
    click.echo("3. Run: dgov run <dir>", err=True)
    raise click.exceptions.Exit(code=1)


def _filter_dag_to_task(dag: DagDefinition, only: str | None) -> DagDefinition:
    if only is None:
        return dag
    if only not in dag.tasks:
        click.echo(f"Error: Task '{only}' not found in plan", err=True)
        raise click.exceptions.Exit(code=1)

    to_keep: set[str] = set()
    queue = [only]
    while queue:
        slug = queue.pop()
        if slug in to_keep or slug not in dag.tasks:
            continue
        to_keep.add(slug)
        queue.extend(dag.tasks[slug].depends_on)
    return dag.model_copy(update={"tasks": {k: v for k, v in dag.tasks.items() if k in to_keep}})


def _emit_run_start(dag_name: str, baseline_quality: int | None) -> None:
    if want_json():
        click.echo(
            json.dumps({
                "status": "starting",
                "dag": dag_name,
                "sentrux_baseline": baseline_quality,
            })
        )
        return
    click.echo(f"[sentrux] Baseline quality: {baseline_quality}")


def _run_plan_runner(runner: EventDagRunner) -> tuple[dict[str, str], timedelta]:
    try:
        start_time = datetime.now(UTC)
        results = asyncio.run(runner.run())
        end_time = datetime.now(UTC)
        return results, end_time - start_time
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        raise click.exceptions.Exit(code=130) from None


def _compile_dag_for_run(plan_file: str, pc: ProjectConfig, only: str | None) -> DagDefinition:
    from dgov.plan import PlanValidationError
    from dgov.types import ConstitutionalViolation

    plan = parse_plan_file(plan_file)
    _ensure_compiled_plan(plan, plan_file)
    try:
        dag = compile_plan(
            plan,
            project_agent=pc.default_agent,
            project_provider=pc.llm_provider,
            provider_agents=pc.provider_default_agents(),
            provider_names=tuple(pc.providers),
            require_provider=True,
            departments=pc.departments,
        )
    except (ConstitutionalViolation, PlanValidationError) as exc:
        raise click.ClickException(str(exc)) from None
    return _filter_dag_to_task(dag, only)


def _make_event_runner(
    dag: DagDefinition,
    *,
    project_root: str,
    stream: bool,
    restart: bool,
    continue_failed: bool,
) -> EventDagRunner:
    return EventDagRunner(
        dag,
        session_root=project_root,
        on_event=_make_worker_event_callback(stream=stream),
        restart=restart,
        continue_failed=continue_failed,
    )


@dataclass(frozen=True)
class PlanRunArtifacts:
    runner: EventDagRunner
    results: dict[str, str]
    duration: timedelta
    gate_result: dict[str, object]
    branch_result: dict[str, object]
    completed_gate_result: dict[str, object]
    token_usage: dict[str, tuple[int, int]]
    total_prompt_tokens: int
    total_completion_tokens: int


@dataclass(frozen=True)
class PlanRunSummary:
    run_status: str
    failed: list[str]
    abandoned: list[str]
    skipped: list[str]
    succeeded: list[str]
    task_errors: dict[str, str]
    self_review_degraded: bool = False


def _execute_plan_with_gates(
    *,
    dag: DagDefinition,
    project_root: str,
    pc: ProjectConfig,
    baseline_quality: int | None,
    stream: bool,
    restart: bool,
    continue_failed: bool,
) -> PlanRunArtifacts:
    runner = _make_event_runner(
        dag,
        project_root=project_root,
        stream=stream,
        restart=restart,
        continue_failed=continue_failed,
    )
    _emit_run_start(dag.name, baseline_quality)
    pre_run_head = run_git.git_stdout(project_root, ["rev-parse", "HEAD"])
    results, duration = _run_plan_runner(runner)
    gate_result = _sentrux_compare(project_root, baseline_quality, pre_run_head, pc)
    branch_result = _branch_verification_gate_from_base(project_root, pc, pre_run_head)
    token_usage = cast(dict[str, tuple[int, int]], getattr(runner, "token_usage", {}))
    total_prompt_tokens, total_completion_tokens = run_output.run_token_totals(token_usage)
    return PlanRunArtifacts(
        runner=runner,
        results=results,
        duration=duration,
        gate_result=gate_result,
        branch_result=branch_result,
        completed_gate_result={**gate_result, "branch_verification": branch_result},
        token_usage=token_usage,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
    )


def _record_run_completion(
    *,
    project_root: str,
    dag: DagDefinition,
    plan_file: str,
    artifacts: PlanRunArtifacts,
    summary: PlanRunSummary,
    only: str | None,
    plan_dir: Path | None,
) -> None:
    _append_run_log(
        project_root,
        dag.name,
        plan_file,
        artifacts.results,
        artifacts.gate_result,
        artifacts.branch_result,
        artifacts.duration,
        artifacts.runner.task_durations,
        summary.task_errors,
        artifacts.total_prompt_tokens,
        artifacts.total_completion_tokens,
    )
    run_lifecycle.maybe_archive_completed_plan(
        run_status=summary.run_status,
        only=only,
        plan_dir=plan_dir,
        project_root=project_root,
        plan_name=dag.name,
        task_slugs=set(dag.tasks),
    )
    run_record.emit_run_completed(
        project_root=project_root,
        plan_name=dag.name,
        run_status=summary.run_status,
        duration=artifacts.duration,
        gate_result=artifacts.completed_gate_result,
        run_source=_runner_run_source(artifacts.runner),
    )


def _runner_run_source(runner: object) -> str:
    run_source = getattr(runner, "run_source", None)
    if isinstance(run_source, str):
        return run_source
    return current_run_source()


def _emit_run_summary_output(
    *,
    run_status: str,
    succeeded: list[str],
    failed: list[str],
    abandoned: list[str],
    skipped: list[str],
    task_errors: dict[str, str],
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    duration: timedelta,
    total_prompt_tokens: int,
    total_completion_tokens: int,
    verbose: bool,
    runner: EventDagRunner,
    token_usage: dict[str, tuple[int, int]],
    results: dict[str, str],
    stream: bool,
    plan_dir: Path | None,
    plan_file: str,
) -> None:
    output_data = run_output.run_output_data(
        run_status=run_status,
        succeeded=succeeded,
        failed=failed,
        abandoned=abandoned,
        skipped=skipped,
        task_errors=task_errors,
        gate_result=gate_result,
        branch_result=branch_result,
        duration=duration,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
    )
    run_output.emit_run_output_data(output_data)
    run_output.emit_verbose_task_durations(
        verbose=verbose,
        task_durations=runner.task_durations,
        token_usage=token_usage,
        results=results,
    )
    run_output.emit_post_run_hint(stream=stream, plan_dir=plan_dir, plan_file=plan_file)


def _summarize_plan_run(artifacts: PlanRunArtifacts) -> PlanRunSummary:
    failed_now = [s for s, st in artifacts.results.items() if st == "failed"]
    task_errors = {
        slug: err for slug, err in artifacts.runner.task_errors.items() if slug in failed_now
    }
    sr_degraded = getattr(artifacts.runner, "self_review_degraded", False)
    run_status, failed, abandoned, skipped, succeeded, _ = run_output.run_status_and_summary(
        artifacts.results,
        task_errors,
        artifacts.gate_result,
        artifacts.branch_result,
        artifacts.duration,
    )
    run_status = run_output.apply_self_review_degraded(run_status, sr_degraded)
    return PlanRunSummary(
        run_status=run_status,
        failed=failed,
        abandoned=abandoned,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
        self_review_degraded=sr_degraded,
    )


def _emit_plan_run_summary(
    *,
    summary: PlanRunSummary,
    artifacts: PlanRunArtifacts,
    verbose: bool,
    stream: bool,
    plan_dir: Path | None,
    plan_file: str,
) -> None:
    run_output.emit_run_warnings(
        failed=summary.failed,
        abandoned=summary.abandoned,
        skipped=summary.skipped,
        succeeded=summary.succeeded,
        task_errors=summary.task_errors,
        gate_result=artifacts.gate_result,
        branch_result=artifacts.branch_result,
        duration=artifacts.duration,
    )
    if summary.self_review_degraded:
        run_output.emit_self_review_degraded_warning()
    _emit_run_summary_output(
        run_status=summary.run_status,
        succeeded=summary.succeeded,
        failed=summary.failed,
        abandoned=summary.abandoned,
        skipped=summary.skipped,
        task_errors=summary.task_errors,
        gate_result=artifacts.gate_result,
        branch_result=artifacts.branch_result,
        duration=artifacts.duration,
        total_prompt_tokens=artifacts.total_prompt_tokens,
        total_completion_tokens=artifacts.total_completion_tokens,
        verbose=verbose,
        runner=artifacts.runner,
        token_usage=artifacts.token_usage,
        results=artifacts.results,
        stream=stream,
        plan_dir=plan_dir,
        plan_file=plan_file,
    )


def _record_plan_run(
    *,
    project_root: str,
    dag: DagDefinition,
    plan_file: str,
    artifacts: PlanRunArtifacts,
    summary: PlanRunSummary,
    only: str | None,
    plan_dir: Path | None,
) -> None:
    _record_run_completion(
        project_root=project_root,
        dag=dag,
        plan_file=plan_file,
        artifacts=artifacts,
        summary=summary,
        only=only,
        plan_dir=plan_dir,
    )


def _raise_on_unsuccessful_run(run_status: str, branch_result: dict[str, object]) -> None:
    if run_status in ("failed", "partial") or run_output.branch_verification_failed(branch_result):
        raise click.exceptions.Exit(code=1)


def _finalize_plan_run(
    artifacts: PlanRunArtifacts,
    *,
    dag: DagDefinition,
    project_root: str,
    plan_file: str,
    only: str | None,
    plan_dir: Path | None,
    verbose: bool,
    stream: bool,
) -> str:
    """Summarize, emit, record, and validate a completed plan run."""
    summary = _summarize_plan_run(artifacts)
    run_lifecycle.maybe_refresh_sentrux_baseline(
        run_status=summary.run_status,
        gate_result=artifacts.gate_result,
        branch_result=artifacts.branch_result,
        only=only,
        plan_dir=plan_dir,
        project_root=project_root,
        plan_name=dag.name,
        task_slugs=set(dag.tasks),
        run_sentrux=run_sentrux,
    )
    _emit_plan_run_summary(
        summary=summary,
        artifacts=artifacts,
        verbose=verbose,
        stream=stream,
        plan_dir=plan_dir,
        plan_file=plan_file,
    )
    _record_plan_run(
        project_root=project_root,
        dag=dag,
        plan_file=plan_file,
        artifacts=artifacts,
        summary=summary,
        only=only,
        plan_dir=plan_dir,
    )
    _raise_on_unsuccessful_run(summary.run_status, artifacts.branch_result)
    return summary.run_status


def run_compiled_plan(
    plan_file: str,
    project_root: str,
    restart: bool = False,
    continue_failed: bool = False,
    only: str | None = None,
    plan_dir: Path | None = None,
    yes: bool = False,
    stream: bool = False,
    verbose: bool = False,
) -> str:
    """Execute a plan TOML with Sentrux quality gates."""
    project_root_path, resolved_plan_file, resolved_plan_dir = _resolve_compiled_run_paths(
        project_root,
        plan_file,
        plan_dir,
    )
    project_root = str(project_root_path)
    pc = load_project_config_or_exit(project_root)
    dag = _compile_dag_for_run(str(resolved_plan_file), pc, only)
    run_git.ensure_git_ready(project_root, yes=yes)
    baseline_quality = _require_sentrux_baseline(project_root)
    artifacts = _execute_plan_with_gates(
        dag=dag,
        project_root=project_root,
        pc=pc,
        baseline_quality=baseline_quality,
        stream=stream,
        restart=restart,
        continue_failed=continue_failed,
    )
    return _finalize_plan_run(
        artifacts,
        dag=dag,
        project_root=project_root,
        plan_file=str(resolved_plan_file),
        only=only,
        plan_dir=resolved_plan_dir,
        verbose=verbose,
        stream=stream,
    )


def _resolve_compiled_run_paths(
    project_root: str,
    plan_file: str,
    plan_dir: Path | None,
) -> tuple[Path, Path, Path | None]:
    project_root_path = Path(project_root).resolve()
    try:
        resolved_plan_file = resolve_project_path(project_root_path, plan_file, label="plan file")
        resolved_plan_dir = (
            resolve_project_path(project_root_path, plan_dir, label="plan directory")
            if plan_dir is not None
            else None
        )
    except ProjectPathError as exc:
        raise click.ClickException(str(exc)) from None
    return project_root_path, resolved_plan_file, resolved_plan_dir


def _append_run_log(
    project_root: str,
    plan_name: str,
    plan_file: str,
    results: dict[str, str],
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    duration: timedelta,
    task_durations: dict[str, float] | None = None,
    task_errors: dict[str, str] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Append a run summary to .dgov/runs.log — permanent, git-tracked."""
    log_path = Path(project_root) / ".dgov" / "runs.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    failed, abandoned, _, merged = run_output.classify_task_results(results)
    status = run_output.run_log_status(
        failed=failed,
        abandoned=abandoned,
        gate_result=gate_result,
        branch_result=branch_result,
    )

    lines = [
        f"[{ts}] {plan_name} ({plan_file}) — {status} ({round(duration.total_seconds(), 2)}s)"
    ]
    if merged:
        lines.append(f"  merged: {', '.join(merged)}")
    if failed:
        lines.append(f"  failed: {', '.join(failed)}")
    if abandoned:
        lines.append(f"  abandoned: {', '.join(abandoned)}")
    run_output.append_task_error_lines(lines, task_errors)
    run_output.append_token_usage_lines(lines, prompt_tokens, completion_tokens)
    run_output.append_task_duration_line(lines, task_durations)
    run_output.append_sentrux_log_lines(lines, gate_result)
    run_output.append_branch_verification_log_lines(lines, branch_result)
    lines.append("")

    with log_path.open("a") as f:
        f.write("\n".join(lines) + "\n")
