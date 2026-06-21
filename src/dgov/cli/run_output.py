"""Run status classification and output rendering helpers."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import click

from dgov.cli import _output, run_checks, want_json


def classify_task_results(
    results: dict[str, str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    failed = [slug for slug, status in results.items() if status == "failed"]
    abandoned = [slug for slug, status in results.items() if status in ("abandoned", "timed_out")]
    skipped = [slug for slug, status in results.items() if status == "skipped"]
    succeeded = [slug for slug, status in results.items() if status == "merged"]
    return failed, abandoned, skipped, succeeded


def sentrux_failed(gate_result: dict[str, object]) -> bool:
    return bool(gate_result.get("degradation")) or bool(gate_result.get("error"))


def branch_verification_failed(branch_result: dict[str, object]) -> bool:
    return branch_result.get("status") == "failed"


def derive_run_status(
    *,
    failed: list[str],
    abandoned: list[str],
    succeeded: list[str],
    sentrux_failed: bool,
) -> str:
    if not failed and not abandoned and not sentrux_failed:
        return "complete"
    if sentrux_failed and not failed and not abandoned:
        return "degraded"
    if succeeded:
        return "partial"
    return "failed"


def stale_run_state(
    *,
    duration: timedelta,
    failed: list[str],
    skipped: list[str],
    succeeded: list[str],
    task_errors: dict[str, str],
) -> bool:
    return bool(
        duration.total_seconds() < 1.0
        and bool(failed or skipped)
        and not succeeded
        and not task_errors
    )


def emit_stale_run_warning() -> None:
    click.echo(
        "No tasks were dispatched — prior run state is still in the database.",
        err=True,
    )
    click.echo("  To retry failed tasks:  dgov run --continue <plan>", err=True)
    click.echo("  To start fresh:         dgov run --restart <plan>", err=True)


def emit_sentrux_warning(gate_result: dict[str, object]) -> None:
    sentrux_message = gate_result.get("error") or "Architectural degradation detected."
    click.echo(f"  sentrux: {sentrux_message}", err=True)
    offenders = gate_result.get("structural_offenders")
    if isinstance(offenders, dict):
        report = run_checks.format_offender_report(cast("dict[object, object]", offenders))
        click.echo(report, err=True)


def run_log_status(
    *,
    failed: list[str],
    abandoned: list[str],
    gate_result: dict[str, object],
    branch_result: dict[str, object],
) -> str:
    post_run_failed = sentrux_failed(gate_result) or branch_verification_failed(branch_result)
    if post_run_failed and not failed and not abandoned:
        return "warn"
    return "ok" if not failed and not abandoned else "fail"


def append_task_error_lines(lines: list[str], task_errors: dict[str, str] | None) -> None:
    if not task_errors:
        return
    for slug, err in task_errors.items():
        lines.append(f"    error[{slug}]: {err[:200]}")


def append_task_duration_line(lines: list[str], task_durations: dict[str, float] | None) -> None:
    if not task_durations:
        return
    dur_str = ", ".join(f"{slug}: {duration}s" for slug, duration in task_durations.items())
    lines.append(f"  durations: {dur_str}")


def format_token_totals(prompt_tokens: int, completion_tokens: int) -> str:
    return f"{prompt_tokens:,} prompt + {completion_tokens:,} completion"


def append_token_usage_lines(
    lines: list[str],
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    lines.append(f"  prompt_tokens: {prompt_tokens:,}")
    lines.append(f"  completion_tokens: {completion_tokens:,}")


def append_sentrux_log_lines(lines: list[str], gate_result: dict[str, object]) -> None:
    quality_before = gate_result.get("quality_before")
    quality_after = gate_result.get("quality_after")
    if quality_before is not None:
        lines.append(f"  sentrux: {quality_before} -> {quality_after}")
    if gate_result.get("degradation"):
        lines.append("  sentrux_status: degradation")
    if gate_result.get("error"):
        lines.append(f"  sentrux_error: {str(gate_result['error'])[:200]}")
    offenders = gate_result.get("structural_offenders")
    if not isinstance(offenders, dict):
        return
    summary = run_checks.format_offender_report(cast("dict[object, object]", offenders)).replace(
        "\n", " | "
    )
    lines.append(f"  sentrux_offenders: {summary[:400]}")


def append_branch_verification_log_lines(
    lines: list[str],
    branch_result: dict[str, object],
) -> None:
    status = branch_result.get("status")
    if not status:
        return
    lines.append(f"  branch_verification_status: {status}")
    if branch_result.get("changed_files") is not None:
        lines.append(f"  branch_verification_changed_files: {branch_result['changed_files']}")
    if branch_result.get("error"):
        lines.append(f"  branch_verification_error: {str(branch_result['error'])[:400]}")


def run_status_and_summary(
    results: dict[str, str],
    task_errors: dict[str, str],
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    duration: timedelta,
) -> tuple[str, list[str], list[str], list[str], list[str], bool]:
    failed, abandoned, skipped, succeeded = classify_task_results(results)
    sentrux_failure = sentrux_failed(gate_result) or branch_verification_failed(branch_result)
    run_status = derive_run_status(
        failed=failed,
        abandoned=abandoned,
        succeeded=succeeded,
        sentrux_failed=sentrux_failure,
    )
    stale_state = stale_run_state(
        duration=duration,
        failed=failed,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
    )
    return run_status, failed, abandoned, skipped, succeeded, stale_state


def emit_run_warnings(
    *,
    failed: list[str],
    abandoned: list[str],
    skipped: list[str],
    succeeded: list[str],
    task_errors: dict[str, str],
    gate_result: dict[str, object],
    branch_result: dict[str, object],
    duration: timedelta,
) -> None:
    if want_json():
        return
    if stale_run_state(
        duration=duration,
        failed=failed,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
    ):
        emit_stale_run_warning()
    for slug, err in task_errors.items():
        click.echo(f"  {slug}: {err[:200]}")
    if abandoned:
        click.echo(
            f"  {len(abandoned)} task(s) abandoned from a prior crashed run. "
            "Use `dgov run --continue` to retry them.",
            err=True,
        )
    if sentrux_failed(gate_result):
        emit_sentrux_warning(gate_result)
    if branch_verification_failed(branch_result):
        click.echo(
            f"  branch verification: {branch_result.get('error', 'failed')}",
            err=True,
        )


def emit_verbose_task_durations(
    *,
    verbose: bool,
    task_durations: dict[str, float],
    token_usage: dict[str, tuple[int, int]],
    results: dict[str, str],
) -> None:
    if not verbose or want_json() or not task_durations:
        return
    click.echo("  per-task:", err=True)
    for slug in sorted(task_durations):
        status = results.get(slug, "?")
        line = f"    {slug}: {task_durations[slug]}s"
        if slug in token_usage:
            prompt_tokens, completion_tokens = token_usage[slug]
            line = f"{line}  ({prompt_tokens:,} + {completion_tokens:,} tokens)"
        click.echo(f"{line}  {status}", err=True)


def emit_post_run_hint(
    *,
    stream: bool,
    plan_dir: Path | None,
    plan_file: str,
) -> None:
    if stream or want_json():
        return
    hint_target = str(plan_dir) if plan_dir is not None else plan_file
    click.echo(
        f"  Live stream: dgov watch   |   Debrief: dgov plan review {hint_target}",
        err=True,
    )


def run_token_totals(token_usage: dict[str, tuple[int, int]]) -> tuple[int, int]:
    total_prompt_tokens = sum(prompt for prompt, _ in token_usage.values())
    total_completion_tokens = sum(completion for _, completion in token_usage.values())
    return total_prompt_tokens, total_completion_tokens


def run_output_data(
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
) -> dict[str, object]:
    return {
        "status": run_status,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "abandoned": len(abandoned) if abandoned else None,
        "skipped": len(skipped) if skipped else None,
        "failed_tasks": failed if failed else None,
        "abandoned_tasks": abandoned if abandoned else None,
        "task_errors": task_errors if task_errors else None,
        "sentrux": gate_result,
        "branch_verification": branch_result,
        "duration_s": round(duration.total_seconds(), 2),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
    }


def emit_run_output_data(output_data: dict[str, object]) -> None:
    if want_json():
        _output(output_data)
        return
    hidden_human_fields = {"total_prompt_tokens", "total_completion_tokens"}
    _output({k: v for k, v in output_data.items() if k not in hidden_human_fields})
    prompt_tokens = cast(int, output_data["total_prompt_tokens"])
    completion_tokens = cast(int, output_data["total_completion_tokens"])
    click.echo(f"tokens: {format_token_totals(prompt_tokens, completion_tokens)}")
